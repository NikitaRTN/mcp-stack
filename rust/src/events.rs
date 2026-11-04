//! Bounded event fan-out for the future SSE panel. No network listener is exposed here.
use serde_json::{json, Value};
use std::collections::{HashMap, VecDeque};
use std::sync::{mpsc, Arc, Mutex, Weak};
use std::time::{SystemTime, UNIX_EPOCH};

const REPLAY_LIMIT: usize = 400;
const SUBSCRIBER_LIMIT: usize = 512;

fn valid_kind(kind: &str) -> bool {
    !kind.is_empty()
        && kind.len() <= 128
        && kind
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"._-".contains(&c))
}

#[derive(Clone, Debug)]
pub struct Event {
    pub seq: u64,
    pub timestamp: f64,
    pub kind: String,
    pub data: Value,
}
impl Event {
    pub fn encode_sse(&self) -> Result<Vec<u8>, String> {
        if !valid_kind(&self.kind) {
            return Err("invalid event kind".into());
        }
        let payload = serde_json::to_string(&json!({
            "seq": self.seq, "ts": self.timestamp, "kind": self.kind, "data": self.data,
        }))
        .map_err(|_| "failed to encode event")?;
        Ok(format!(
            "id: {}\nevent: {}\ndata: {}\n\n",
            self.seq, self.kind, payload
        )
        .into_bytes())
    }
}

struct Inner {
    sequence: u64,
    subscriber_id: u64,
    subscribers: HashMap<u64, mpsc::SyncSender<Event>>,
    history: VecDeque<Event>,
}

#[derive(Clone)]
pub struct EventBus {
    inner: Arc<Mutex<Inner>>,
    replay_limit: usize,
    subscriber_limit: usize,
}

impl Default for EventBus {
    fn default() -> Self {
        Self::new(REPLAY_LIMIT, SUBSCRIBER_LIMIT)
    }
}
impl EventBus {
    pub fn new(replay_limit: usize, subscriber_limit: usize) -> Self {
        assert!(replay_limit > 0 && subscriber_limit > 0);
        Self {
            inner: Arc::new(Mutex::new(Inner {
                sequence: 0,
                subscriber_id: 0,
                subscribers: HashMap::new(),
                history: VecDeque::new(),
            })),
            replay_limit,
            subscriber_limit,
        }
    }
    pub fn publish(&self, kind: &str, data: Value) -> Result<Event, &'static str> {
        // Prevent SSE field injection through the event name.
        if !valid_kind(kind) {
            return Err("invalid event kind");
        }
        let mut inner = self.inner.lock().map_err(|_| "event bus lock poisoned")?;
        inner.sequence = inner
            .sequence
            .checked_add(1)
            .ok_or("event sequence exhausted")?;
        let event = Event {
            seq: inner.sequence,
            timestamp: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs_f64(),
            kind: kind.into(),
            data,
        };
        inner.history.push_back(event.clone());
        if inner.history.len() > self.replay_limit {
            inner.history.pop_front();
        }
        inner
            .subscribers
            .retain(|_, sender| sender.try_send(event.clone()).is_ok());
        Ok(event)
    }
    /// Subscription and replay are captured under one lock to avoid a delivery gap.
    /// last_seq=0 is a fresh subscription and does not request backlog.
    pub fn subscribe(&self, last_seq: u64) -> Result<(Subscription, Vec<Event>), &'static str> {
        let mut inner = self.inner.lock().map_err(|_| "event bus lock poisoned")?;
        inner.subscriber_id = inner
            .subscriber_id
            .checked_add(1)
            .ok_or("subscriber ID exhausted")?;
        let id = inner.subscriber_id;
        let backlog = if last_seq == 0 {
            Vec::new()
        } else {
            inner
                .history
                .iter()
                .filter(|e| e.seq > last_seq)
                .cloned()
                .collect()
        };
        let (sender, receiver) = mpsc::sync_channel(self.subscriber_limit);
        inner.subscribers.insert(id, sender);
        Ok((
            Subscription {
                id,
                receiver,
                inner: Arc::downgrade(&self.inner),
            },
            backlog,
        ))
    }
    pub fn listeners(&self) -> usize {
        self.inner
            .lock()
            .map(|state| state.subscribers.len())
            .unwrap_or(0)
    }
}

pub struct Subscription {
    id: u64,
    pub receiver: mpsc::Receiver<Event>,
    inner: Weak<Mutex<Inner>>,
}
impl Subscription {
    /// An overflowing subscriber is detached; SSE handlers must close that stream.
    pub fn is_active(&self) -> bool {
        self.inner
            .upgrade()
            .and_then(|inner| {
                inner
                    .lock()
                    .ok()
                    .map(|s| s.subscribers.contains_key(&self.id))
            })
            .unwrap_or(false)
    }
}
impl Drop for Subscription {
    fn drop(&mut self) {
        if let Some(inner) = self.inner.upgrade() {
            if let Ok(mut state) = inner.lock() {
                state.subscribers.remove(&self.id);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn replay_is_bounded_and_subscription_has_no_gap() {
        let bus = EventBus::new(2, 4);
        for n in 0..3 {
            bus.publish("update", json!({"n":n})).unwrap();
        }
        let (sub, backlog) = bus.subscribe(1).unwrap();
        assert_eq!(backlog.iter().map(|e| e.seq).collect::<Vec<_>>(), [2, 3]);
        bus.publish("update", json!({"n":3})).unwrap();
        assert_eq!(sub.receiver.try_recv().unwrap().seq, 4);
        assert!(sub.is_active());
    }
    #[test]
    fn full_subscriber_is_detached_without_blocking_others() {
        let bus = EventBus::new(2, 1);
        let (slow, _) = bus.subscribe(0).unwrap();
        let (fast, _) = bus.subscribe(0).unwrap();
        bus.publish("test", json!({})).unwrap();
        assert_eq!(fast.receiver.try_recv().unwrap().seq, 1);
        bus.publish("test", json!({})).unwrap();
        assert!(!slow.is_active());
        assert!(fast.is_active());
        assert_eq!(fast.receiver.try_recv().unwrap().seq, 2);
        assert_eq!(bus.listeners(), 1);
        drop(fast);
        assert_eq!(bus.listeners(), 0);
    }
    #[test]
    fn fresh_subscription_has_no_backlog() {
        let bus = EventBus::default();
        bus.publish("first", json!({})).unwrap();
        let (_, backlog) = bus.subscribe(0).unwrap();
        assert!(backlog.is_empty());
    }
    #[test]
    fn encodes_event_without_allowing_field_injection() {
        let bus = EventBus::default();
        assert!(bus.publish("bad\nevent: stolen", json!({})).is_err());
        let e = bus
            .publish("service.changed", json!({"text":"a\nb"}))
            .unwrap();
        let encoded = String::from_utf8(e.encode_sse().unwrap()).unwrap();
        assert!(encoded.starts_with("id: 1\nevent: service.changed\ndata: "));
        assert!(encoded.ends_with("\n\n"));
        assert!(encoded.contains("a\\nb"));
    }
}
