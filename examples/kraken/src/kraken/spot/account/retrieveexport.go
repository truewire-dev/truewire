package account

import (
	"context"

	truewire "truewire.dev/core"
)

// RetrieveExport downloads a finished export report as the zip archive's bytes.
//
// Hand-written (the spec's `surface`): the reply is a binary archive, not a value a schema
// describes, so no method is generated. It lives beside the generated router and reaches
// the core the router keeps; the core's REST transport provides the raw download.
func (r *Account) RetrieveExport(ctx context.Context, id string) ([]byte, error) {
	downloader, ok := r.core.(interface {
		RetrieveExport(ctx context.Context, id string) ([]byte, error)
	})
	if !ok {
		return nil, truewire.LogicError("the spot core %T cannot download export archives", r.core)
	}
	return downloader.RetrieveExport(ctx, id)
}
