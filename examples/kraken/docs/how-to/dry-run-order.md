# Check an order before placing it

Kraken's `AddOrder` takes its own `validate` field. When it is true, Kraken checks the
order (pair, volume, price, your permissions and balance) and describes it, but never
sends it to the matching engine. The response then carries the order's description and
no `txid`.

The order is a typed dict whose `ordertype` picks its shape, so a limit order without a
`price` is a type error before it is a request:

```python
from kraken import Kraken
from kraken.spot.trading.add_order import AddOrderLimit


async def check_order() -> None:
  order: AddOrderLimit = {
    'pair': 'XBTUSD',
    'type': 'buy',
    'ordertype': 'limit',
    'volume': '0.0001',
    'price': '10000',
    'validate': True,
  }
  async with Kraken.new() as client:
    result = await client.spot.trading.add_order(order)
    description = result.get('descr')
    if description is not None:
      print(description.get('order'))
    assert 'txid' not in result
```

Two different flags are named `validate` here. The one inside the order is Kraken's
dry-run flag and goes over the wire. The method's own `validate=` argument (default
`True`) says whether the client checks the response against its schema.

The key needs **Orders and trades — Create & modify orders** even for a dry run (see
[API keys](../api-keys.md)).
