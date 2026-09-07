import base64

from pydantic import BaseModel, Field
from pydantic import model_validator
from typing_extensions import Any, Literal


PROTOBUF_MEDIA_TYPE = 'application/x-protobuf'
JSON_MEDIA_TYPE = 'application/json'


class ExampleRequest(BaseModel):
  """Invocation data stored in `*.request.json` example files."""
  description: str | None = None
  request: dict[str, Any] | None = None
  """Single value matching the endpoint's `request` schema, superseding
  `parameters`/`payload` below -- kept as legacy until every project has migrated."""
  parameters: dict[str, Any] = Field(default_factory=dict)
  payload: Any | None = None
  args: list[Any] = Field(default_factory=list)
  kwargs: dict[str, Any] = Field(default_factory=dict)

  @model_validator(mode='after')
  def fill_kwargs_from_parameters(self) -> 'ExampleRequest':
    """Keep legacy call helpers compatible with parameter-shaped examples."""
    if not self.kwargs and self.parameters:
      self.kwargs = dict(self.parameters)
    return self


class ExampleResponse(BaseModel):
  """HTTP response data stored in `*.response.json` example files."""
  status: int
  payload: Any


class WsParametersExample(BaseModel):
  """WebSocket subscription parameters stored in `*.parameters.json` files."""
  description: str | None = None
  parameters: dict[str, Any] = Field(default_factory=dict)
  payload: dict[str, Any] | None = None


class WsReplyExample(BaseModel):
  """JSON-compatible WebSocket subscription reply example."""
  root: Any

  @classmethod
  def from_payload(cls, payload: Any) -> 'WsReplyExample':
    """Wrap a reply payload from a JSON example file."""
    return cls(root=payload)


class WsMessagesExample(BaseModel):
  """Decoded WebSocket messages stored in `*.messages.json` files."""
  messages: list[Any]

  @classmethod
  def from_payload(cls, payload: Any) -> 'WsMessagesExample':
    """Normalize a single message or message list into a message sequence."""
    return cls(messages=payload if isinstance(payload, list) else [payload])


class EncodedFrame(BaseModel):
  """Base64-encoded binary frame stored in a Protobuf sidecar fixture."""
  content_type: Literal['application/x-protobuf'] = PROTOBUF_MEDIA_TYPE
  encoding: Literal['base64'] = 'base64'
  data: str
  description: str | None = None

  def decode(self) -> bytes:
    """Decode the frame bytes, raising a clear error for invalid base64."""
    return base64.b64decode(self.data, validate=True)


class WsBinaryFramesExample(BaseModel):
  """Binary WebSocket message frames stored as a JSON sidecar."""
  frames: list[EncodedFrame]

  @classmethod
  def from_payload(cls, payload: Any) -> 'WsBinaryFramesExample':
    """Load either a frame list or `{"frames": [...]}` envelope."""
    if isinstance(payload, dict) and 'frames' in payload:
      return cls.model_validate(payload)
    if isinstance(payload, list):
      return cls(frames=[EncodedFrame.model_validate(item) for item in payload])
    return cls(frames=[EncodedFrame.model_validate(payload)])

  def decode(self) -> list[bytes]:
    """Decode every stored binary frame."""
    return [frame.decode() for frame in self.frames]
