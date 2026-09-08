//! A live subscription.

use std::pin::Pin;
use std::task::{Context, Poll};

use futures::future::BoxFuture;
use futures::stream::BoxStream;
use futures::{FutureExt, StreamExt};

use crate::errors::Result;

/// The one-shot unsubscribe a [`Stream`] holds until it is called.
type Unsubscribe<Reply> = Box<dyn FnOnce() -> BoxFuture<'static, Result<Option<Reply>>> + Send>;

/// A live subscription: the reply that acknowledged it, the notifications as they arrive,
/// and [`unsubscribe`](Self::unsubscribe).
///
/// A `Stream<N>` is a `futures::Stream<Item = Result<N>>`: each item is one pushed
/// notification, and the one error item a dropped connection produces ends it. Dropping
/// the value without unsubscribing forgets the subscription locally (pushes are discarded)
/// but sends no unsubscribe frame; call `unsubscribe()` to end it on the wire.
///
/// ```ignore
/// let mut ticker = client.streams.ticker(params).await?;
/// println!("{:?}", ticker.reply);
/// while let Some(message) = ticker.next().await { ... }
/// ticker.unsubscribe().await?;
/// ```
pub struct Stream<N, Reply = serde_json::Value> {
    /// The reply that acknowledged the subscription; `None` when the protocol sends none.
    pub reply: Option<Reply>,
    items: BoxStream<'static, Result<N>>,
    unsubscribe: Option<Unsubscribe<Reply>>,
}

impl<N, Reply> std::fmt::Debug for Stream<N, Reply>
where
    Reply: std::fmt::Debug,
{
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Stream")
            .field("reply", &self.reply)
            .finish_non_exhaustive()
    }
}

impl<N, Reply> Stream<N, Reply>
where
    N: Send + 'static,
    Reply: Send + 'static,
{
    /// Assemble a stream from its parts, the way [`Socket::subscribe`](super::Socket::subscribe) does.
    pub fn new<F, Fut>(reply: Option<Reply>, items: BoxStream<'static, Result<N>>, unsubscribe: F) -> Self
    where
        F: FnOnce() -> Fut + Send + 'static,
        Fut: std::future::Future<Output = Result<Option<Reply>>> + Send + 'static,
    {
        Self {
            reply,
            items,
            unsubscribe: Some(Box::new(move || unsubscribe().boxed())),
        }
    }

    /// Unsubscribe; the reply, or `None` when already unsubscribed or the protocol sends none.
    pub async fn unsubscribe(&mut self) -> Result<Option<Reply>> {
        match self.unsubscribe.take() {
            Some(unsubscribe) => unsubscribe().await,
            None => Ok(None),
        }
    }

    /// The same subscription with every notification passed through `f`: how a generated
    /// stream endpoint turns the wire `Value` into its message type.
    pub fn map<T, F>(self, f: F) -> Stream<T, Reply>
    where
        T: Send + 'static,
        F: Fn(N) -> Result<T> + Send + 'static,
    {
        Stream {
            reply: self.reply,
            items: self.items.map(move |item| item.and_then(&f)).boxed(),
            unsubscribe: self.unsubscribe,
        }
    }

    /// The same subscription keeping only the notifications `f` accepts.
    pub fn filter<F>(self, f: F) -> Self
    where
        F: Fn(&N) -> bool + Send + 'static,
    {
        Stream {
            reply: self.reply,
            items: self
                .items
                .filter(move |item| std::future::ready(item.as_ref().map(&f).unwrap_or(true)))
                .boxed(),
            unsubscribe: self.unsubscribe,
        }
    }
}

impl<N, Reply: Unpin> futures::Stream for Stream<N, Reply> {
    type Item = Result<N>;

    fn poll_next(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        self.get_mut().items.poll_next_unpin(cx)
    }
}
