/** The page's only network: the companion server's event stream and its POST endpoints (SPEC 11.3). */

const RETRY_MS = 1500;

/**
 * Follow /events. `onEvent(event, fresh)` gets each event; `fresh` is true for the first event
 * of every connection, whose replay starts the scene over. `onState` gets 'connecting', 'live'
 * or 'reconnecting'. After an error the stream is closed and reopened after a short delay.
 */
export function follow(onEvent, onState) {
  let source = null;
  let timer = null;
  let fresh = true;
  let openedAt = 0;

  function open() {
    clearTimeout(timer);
    onState('connecting');
    fresh = true;
    source = new EventSource('/events');
    source.onopen = () => {
      openedAt = performance.now();
      onState('live');
    };
    source.onmessage = (message) => {
      let event;
      try {
        event = JSON.parse(message.data);
      } catch {
        return;
      }
      const first = fresh;
      fresh = false;
      onEvent(event, first, performance.now() - openedAt < 400);
    };
    source.onerror = () => {
      source.close();
      onState('reconnecting');
      timer = setTimeout(open, RETRY_MS);
    };
  }

  open();
  return { reconnect: open };
}

/** POST a JSON body; resolves to {ok, status} and never throws. */
export async function post(path, body = {}) {
  try {
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    return { ok: res.ok, status: res.status };
  } catch {
    return { ok: false, status: 0 };
  }
}
