"""`truewire.spec.endpoint`'s `EnvelopeSpec`: declared per-endpoint envelope extraction,
per-kind routing (`selector`/`params` for `rpc`, `channel` for `stream`), and
request/response correlation."""

import pytest
from pydantic import ValidationError

from truewire.spec import (
  Endpoint,
  RpcEnvelopeSpec,
  StreamEnvelopeSpec,
  envelope_spec,
  read_dotted_path,
  write_dotted_path,
)

RPC_HTTP_WITH_ENVELOPE = {
  'function': 'eth.get_balance',
  'spec': {
    'kind': 'rpc',
    'transports': ['http'],
    'path': 'acme_getBalance',
    'method': 'POST',
    'openapi': {'description': 'Balance.', 'responses': {'200': {'description': 'ok'}}},
  },
  'envelope': {'payload': 'result', 'correlate': 'id'},
}

RPC_HTTP_WITHOUT_ENVELOPE = {
  'function': 'portfolio.token_balances',
  'spec': {
    'kind': 'rpc',
    'transports': ['http'],
    'path': '/data/v1/tokens',
    'method': 'POST',
    'openapi': {
      'description': 'Balances.',
      'responses': {'200': {'description': 'ok'}},
    },
  },
}

STREAM_WITH_ENVELOPE = {
  'function': 'kucoin.ticker',
  'spec': {
    'kind': 'stream',
    'channel': '/market/ticker:{symbol}',
    'openapi': {'description': 'Ticker.', 'responses': {'message': {'description': 'ok'}}},
  },
  'envelope': {'payload': 'data', 'channel': 'topic'},
}


def test_endpoint_without_envelope_field_has_none():
  endpoint = Endpoint.model_validate(RPC_HTTP_WITHOUT_ENVELOPE)
  assert endpoint.envelope is None


def test_endpoint_parses_its_own_envelope_as_rpc_kind():
  endpoint = Endpoint.model_validate(RPC_HTTP_WITH_ENVELOPE)
  assert endpoint.envelope is not None
  assert isinstance(endpoint.envelope, RpcEnvelopeSpec)
  assert endpoint.envelope.payload == 'result'
  assert endpoint.envelope.correlate_paths.request == 'id'
  assert endpoint.envelope.correlate_paths.response == 'id'


def test_endpoint_derives_stream_envelope_kind_from_spec_kind():
  """`kind` is never written in `endpoint.json`; `Endpoint.tag_envelope_kind` derives it
  from the sibling `spec.kind`, dispatching to `StreamEnvelopeSpec` here."""
  endpoint = Endpoint.model_validate(STREAM_WITH_ENVELOPE)
  assert endpoint.envelope is not None
  assert isinstance(endpoint.envelope, StreamEnvelopeSpec)
  assert endpoint.envelope.payload == 'data'
  assert endpoint.envelope.channel == 'topic'


def test_correlate_shorthand_normalizes_to_matching_paths():
  envelope = RpcEnvelopeSpec.model_validate({'payload': 'result', 'correlate': 'id'})
  assert envelope.correlate_paths.request == 'id'
  assert envelope.correlate_paths.response == 'id'


def test_correlate_accepts_distinct_request_response_paths():
  envelope = RpcEnvelopeSpec.model_validate(
    {
      'payload': 'result',
      'correlate': {'request': 'reqId', 'response': 'id'},
    }
  )
  assert envelope.correlate_paths.request == 'reqId'
  assert envelope.correlate_paths.response == 'id'


def test_envelope_without_correlate_has_no_correlate_paths():
  envelope = RpcEnvelopeSpec.model_validate({'payload': 'result'})
  assert envelope.correlate_paths is None


def test_envelope_rejects_unknown_fields():
  with pytest.raises(ValidationError):
    RpcEnvelopeSpec.model_validate({'payload': 'result', 'unexpected': True})


def test_rpc_envelope_rejects_channel_field():
  """`channel` is stream-only; a `kind: 'rpc'` envelope declaring it is a validation error,
  not a silently-ignored field."""
  with pytest.raises(ValidationError):
    RpcEnvelopeSpec.model_validate({'payload': 'result', 'channel': 'topic'})


def test_stream_envelope_rejects_selector_field():
  """`selector`/`params` are rpc-only; a `kind: 'stream'` envelope declaring either is a
  validation error, not a silently-ignored field."""
  with pytest.raises(ValidationError):
    StreamEnvelopeSpec.model_validate({'payload': 'data', 'selector': 'op'})


def test_rpc_envelope_accepts_selector_and_params():
  envelope = RpcEnvelopeSpec.model_validate(
    {'payload': 'data', 'selector': 'op', 'params': 'args'}
  )
  assert envelope.selector == 'op'
  assert envelope.params == 'args'


def test_rpc_envelope_selector_and_params_default_to_none():
  envelope = RpcEnvelopeSpec.model_validate({'payload': 'result'})
  assert envelope.selector is None
  assert envelope.params is None


def test_stream_envelope_accepts_channel():
  envelope = StreamEnvelopeSpec.model_validate({'payload': 'data', 'channel': 'topic'})
  assert envelope.channel == 'topic'


def test_stream_envelope_channel_defaults_to_none():
  envelope = StreamEnvelopeSpec.model_validate({'payload': 'data'})
  assert envelope.channel is None


def test_extract_value_reads_nested_path():
  envelope = RpcEnvelopeSpec.model_validate({'payload': 'data.result'})
  assert envelope.extract_value({'data': {'result': {'a': 1}}}) == {'a': 1}


def test_extract_value_returns_none_when_absent():
  envelope = RpcEnvelopeSpec.model_validate({'payload': 'result'})
  assert envelope.extract_value({'other': 1}) is None


def test_envelope_spec_factory_dispatches_by_spec_kind():
  """`envelope_spec` is what call sites use when they only have the raw `envelope` object
  and the sibling `spec.kind` -- not an `Endpoint` to parse -- since `endpoint.json` never
  writes `kind` inside `envelope` itself."""
  rpc = envelope_spec({'payload': 'result', 'selector': 'op'}, spec_kind='rpc')
  assert isinstance(rpc, RpcEnvelopeSpec)
  assert rpc.selector == 'op'

  stream = envelope_spec({'payload': 'data', 'channel': 'topic'}, spec_kind='stream')
  assert isinstance(stream, StreamEnvelopeSpec)
  assert stream.channel == 'topic'


def test_read_dotted_path_and_write_dotted_path_round_trip():
  value = {'jsonrpc': '2.0', 'id': 1, 'result': {'a': 1}}
  assert read_dotted_path(value, 'id') == 1
  updated = write_dotted_path(value, 'id', 99)
  assert updated['id'] == 99
  assert value['id'] == 1  # original untouched


def test_read_dotted_path_with_empty_path_returns_the_whole_value():
  """`''` is the whole-frame sentinel: nothing to index into, return `value` unread."""
  value = {'id': 1, 'type': 'ack'}
  assert read_dotted_path(value, '') is value


def test_payload_accepts_empty_string_as_the_whole_frame():
  envelope = RpcEnvelopeSpec.model_validate({'payload': ''})
  assert envelope.payload == ''


def test_extract_value_with_empty_payload_returns_the_whole_frame():
  """kucoin's flat subscribe ack `{id, type}` has nothing worth extracting -- `payload: ''`
  validates the whole frame instead of forcing a single field to stand in for it."""
  envelope = RpcEnvelopeSpec.model_validate({'payload': ''})
  assert envelope.extract_value({'id': 1, 'type': 'ack'}) == {'id': 1, 'type': 'ack'}


def test_stream_reply_payload_accepts_empty_string_as_the_whole_ack_frame():
  envelope = StreamEnvelopeSpec.model_validate(
    {'payload': 'data', 'channel': 'topic', 'reply_payload': ''}
  )
  assert envelope.reply_payload == ''


def test_payload_still_rejects_a_malformed_non_empty_path():
  """The empty-string sentinel is additive -- every other invalid path still rejects.

  `result[0]` is no longer one of those -- `docs/pagination.md` §3 admits bracket indices
  fleet-wide now -- so this uses a wildcard instead, which stays refused."""
  with pytest.raises(ValidationError):
    RpcEnvelopeSpec.model_validate({'payload': 'result.*'})
