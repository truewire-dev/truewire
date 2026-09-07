# Get Order Amends

`POST /private/OrderAmends` -- retrieves an audit trail of amend transactions for one
order, ordered by ascending amend timestamp. The first entry always has `amend_type:
"original"` and carries the order's original parameters. Requires
`Orders and trades - Query open orders & trades` or
`Orders and trades - Query closed orders & trades`, depending on the order's status.

## Example response

```json
{
  "error": [],
  "result": {
    "amends": [
      {"amend_id": "TSUN4B-EX2XN-WQ6GKG", "amend_type": "original", "order_qty": "0.01000000", "remaining_qty": "0.01000000", "limit_price": "61032.8", "timestamp": 1724158070287558000},
      {"amend_id": "TF6VAW-VUWMX-6SXTCH", "amend_type": "user", "order_qty": "0.01000000", "remaining_qty": "0.01000000", "limit_price": "61032.7", "timestamp": 1724158076936755700}
    ],
    "count": 3
  }
}
```

Reference: [Get Order Amends](https://docs.kraken.com/api-reference/account-data/get-order-amends)
