// Package protoframes decodes protobuf-framed WebSocket pushes (ADR 0016) against the
// project's `spec/proto/*.proto` sources, and reads them as canonical ProtoJSON.
//
// The sources travel with the generated package (`<package>/protos`, `protos.Sources`) and
// are compiled at run time, so a core needs no protoc step:
//
//	frames := protoframes.MustCompile(protos.Sources, "PushDataV3ApiWrapper")
//
//	streams.Parse = func(d ws.Data) (string, *protoframes.Frame, bool, error) {
//		if d.Type != ws.Binary {
//			return "", nil, false, nil // a JSON ack, handled elsewhere
//		}
//		frame, err := frames.Decode(d.Bytes)
//		if err != nil {
//			return "", nil, false, err
//		}
//		return frame.String("channel"), frame, true, nil
//	}
//
//	// per endpoint: narrow to meta.ProtoField
//	body, ok, err := frame.Field(meta.ProtoField)
//
// Every value handed out is ProtoJSON (lowerCamelCase json names, 64-bit integers as
// strings, bytes as base64, enums by name, unset fields left out), the rendering the
// TypeScript and Rust cores produce for the same frame. Fields are addressed by their
// `.proto` name (`public_aggre_deals`), the name `meta.proto_field` carries.
package protoframes

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"sort"

	"github.com/bufbuild/protocompile"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
	"google.golang.org/protobuf/types/dynamicpb"

	truewire "truewire.dev/core"
)

// Frames is one message type compiled from the project's sources: the frame envelope.
type Frames struct {
	Message protoreflect.MessageDescriptor
}

// Compile compiles sources (file name -> `.proto` text; imports between them resolve by
// file name, and the well-known `google/protobuf/*.proto` files are available) and looks up
// message by its full name. A LogicError when the sources do not compile or name no such
// message.
func Compile(sources map[string]string, message string) (*Frames, error) {
	names := make([]string, 0, len(sources))
	for name := range sources {
		names = append(names, name)
	}
	sort.Strings(names)
	compiler := protocompile.Compiler{
		Resolver: protocompile.WithStandardImports(&protocompile.SourceResolver{
			Accessor: protocompile.SourceAccessorFromMap(sources),
		}),
	}
	files, err := compiler.Compile(context.Background(), names...)
	if err != nil {
		return nil, truewire.LogicError("protobuf sources do not compile: %v", err)
	}
	desc, err := files.AsResolver().FindDescriptorByName(protoreflect.FullName(message))
	if err != nil {
		return nil, truewire.LogicError("protobuf sources declare no %q: %v", message, err)
	}
	md, ok := desc.(protoreflect.MessageDescriptor)
	if !ok {
		return nil, truewire.LogicError("protobuf %q is not a message", message)
	}
	return &Frames{Message: md}, nil
}

// MustCompile is Compile, panicking on error: for package-level variables over generated sources.
func MustCompile(sources map[string]string, message string) *Frames {
	frames, err := Compile(sources, message)
	if err != nil {
		panic(err)
	}
	return frames
}

// Decode decodes one binary frame; a ValidationError for bytes that are not this message.
func (f *Frames) Decode(data []byte) (*Frame, error) {
	msg := dynamicpb.NewMessage(f.Message)
	if err := proto.Unmarshal(data, msg); err != nil {
		return nil, truewire.ValidationError("", fmt.Sprintf("the frame does not decode as %s: %v", f.Message.Name(), err))
	}
	return &Frame{Message: msg}, nil
}

// Frame is one decoded frame.
type Frame struct {
	Message *dynamicpb.Message
}

func (fr *Frame) descriptor(name string) (protoreflect.FieldDescriptor, error) {
	fd := fr.Message.Descriptor().Fields().ByName(protoreflect.Name(name))
	if fd == nil {
		return nil, truewire.LogicError("%s has no field %q", fr.Message.Descriptor().Name(), name)
	}
	return fd, nil
}

// Has reports whether the field (by `.proto` name) is set: a oneof member chosen, a
// repeated field non-empty. False for a field the message does not declare.
func (fr *Frame) Has(name string) bool {
	fd, err := fr.descriptor(name)
	return err == nil && fr.Message.Has(fd)
}

// Field is the field's ProtoJSON value; ok false when it is not set.
func (fr *Frame) Field(name string) (value json.RawMessage, ok bool, err error) {
	fd, err := fr.descriptor(name)
	if err != nil || !fr.Message.Has(fd) {
		return nil, false, err
	}
	if fd.Kind() == protoreflect.MessageKind && !fd.IsList() && !fd.IsMap() {
		value, err = marshal(fr.Message.Get(fd).Message().Interface())
		return value, err == nil, err
	}
	whole, err := fr.JSON()
	if err != nil {
		return nil, false, err
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(whole, &fields); err != nil {
		return nil, false, err
	}
	value, ok = fields[fd.JSONName()]
	return value, ok, nil
}

// String is a string field's value, "" when unset or not a string.
func (fr *Frame) String(name string) string {
	fd, err := fr.descriptor(name)
	if err != nil || fd.Kind() != protoreflect.StringKind || fd.IsList() || fd.IsMap() {
		return ""
	}
	return fr.Message.Get(fd).String()
}

// OneofCase is the `.proto` name of the member set in oneof, "" when none is.
func (fr *Frame) OneofCase(oneof string) string {
	od := fr.Message.Descriptor().Oneofs().ByName(protoreflect.Name(oneof))
	if od == nil {
		return ""
	}
	if fd := fr.Message.WhichOneof(od); fd != nil {
		return string(fd.Name())
	}
	return ""
}

// JSON is the whole frame as compact ProtoJSON.
func (fr *Frame) JSON() (json.RawMessage, error) {
	return marshal(fr.Message)
}

func marshal(m proto.Message) (json.RawMessage, error) {
	data, err := protojson.Marshal(m)
	if err != nil {
		return nil, err
	}
	var compact bytes.Buffer
	if err := json.Compact(&compact, data); err != nil {
		return nil, err
	}
	return compact.Bytes(), nil
}
