// The WebSocket surface against `truewire mock`: channel subscriptions over each socket,
// the trading methods, and the two whole-frame commands (`ping`, `batch_cancel`) whose
// reply is not nested under `result`. The same walks as `test/test_streams.py` and
// `test/streams.test.ts`, through the Go client.
//
// A subscription is read, never unsubscribed, as in the other languages' tests: the mock
// has no recorded unsubscribe ack for these channels. Unsubscribe is proved by core-go's
// own ws tests.
package tests

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	truewire "truewire.dev/core"

	"truewire.dev/examples/kraken/src/kraken/streams/marketdata/ticker"
	"truewire.dev/examples/kraken/src/kraken/streams/private/balances"
	"truewire.dev/examples/kraken/src/kraken/tradingws/addorder"
	"truewire.dev/examples/kraken/src/kraken/tradingws/batchcancel"
)

func timeout(t *testing.T) context.Context {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	t.Cleanup(cancel)
	return ctx
}

func TestAPublicChannelOverTheMarketDataSocket(t *testing.T) {
	client, transports := mockClient(t)
	ctx := timeout(t)
	stream, err := client.Streams.MarketData.Ticker(ctx, ticker.Parameters{Symbol: []string{"BTC/USD"}})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(stream.Reply.(json.RawMessage)), `"subscribe"`) {
		t.Fatalf("reply %s", stream.Reply)
	}
	if !transports.MarketClient.IsOpen() || transports.PrivateClient.IsOpen() {
		t.Fatal("the public channel uses the market-data socket only")
	}
	message, err := stream.Next(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if message.Channel != "ticker" || message.Type != "snapshot" || message.Data[0].Symbol != "BTC/USD" {
		t.Fatalf("%+v", message)
	}
}

func TestAPrivateChannelOverThePrivateSocket(t *testing.T) {
	client, transports := mockClient(t)
	ctx := timeout(t)
	stream, err := client.Streams.Private.Balances(ctx, balances.Parameters{})
	if err != nil {
		t.Fatal(err)
	}
	message, err := stream.Next(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if message.BalancesSnapshotMessage == nil && message.BalancesUpdateMessage == nil {
		t.Fatal("no balances message")
	}
	if !transports.PrivateClient.IsOpen() || transports.MarketClient.IsOpen() {
		t.Fatal("the private channel uses the private socket only")
	}
}

func TestRawStreamHandsTheFrameOverAsItCame(t *testing.T) {
	client, _ := mockClient(t)
	ctx := timeout(t)
	stream, err := client.Streams.MarketData.TickerRaw(ctx, ticker.Parameters{Symbol: []string{"BTC/USD"}})
	if err != nil {
		t.Fatal(err)
	}
	frame, err := stream.Next(ctx)
	if err != nil {
		t.Fatal(err)
	}
	var decoded struct {
		Channel string `json:"channel"`
		Data    []struct {
			Timestamp any `json:"timestamp"`
		} `json:"data"`
	}
	if err := json.Unmarshal(frame, &decoded); err != nil || decoded.Channel != "ticker" {
		t.Fatalf("%s %v", frame, err)
	}
	if _, isString := decoded.Data[0].Timestamp.(string); !isString {
		t.Fatalf("the raw frame keeps the wire's timestamp string: %s", frame)
	}
}

func TestATradingMethodReplyNestedUnderResult(t *testing.T) {
	client, _ := mockClient(t)
	request, err := truewire.Decode[addorder.Request]([]byte(`{"symbol":"XBT/USDC","side":"buy","order_type":"limit","order_qty":0.0001,"limit_price":10000.0}`))
	if err != nil {
		t.Fatal(err)
	}
	result, err := client.TradingWs.AddOrder(timeout(t), request)
	if err != nil {
		t.Fatal(err)
	}
	if result.OrderID == nil || *result.OrderID == "" {
		t.Fatalf("%+v", result)
	}
}

func TestPingAnswersWithTheWholePongFrame(t *testing.T) {
	client, _ := mockClient(t)
	reply, err := client.Streams.MarketData.Ping(timeout(t))
	if err != nil {
		t.Fatal(err)
	}
	if reply.Method != "pong" || reply.TimeIn.IsZero() {
		t.Fatalf("%+v", reply)
	}
}

func TestBatchCancelReportsAtTheTopLevelOfTheFrame(t *testing.T) {
	client, _ := mockClient(t)
	reply, err := client.TradingWs.BatchCancel(timeout(t), batchcancel.Request{Orders: []string{"OOWVMC-7HDFH-B7UPWM", "O7L6T4-QEGI4-7M4PUY"}})
	if err != nil {
		t.Fatal(err)
	}
	if reply.OrdersCancelled == nil || *reply.OrdersCancelled != 2 {
		t.Fatalf("%+v", reply)
	}
}
