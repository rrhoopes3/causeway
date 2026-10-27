"""Tests for the WebXR ledger lattice (`causeway view --xr`).

The flat viewer stays the default. `--xr` writes a second self-contained page over the same ledger payload.
Voice transcription is not part of this page.
"""

import json
import re
import subprocess
from importlib import resources
from pathlib import Path

from causeway import cli, ledger, view

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = [
    ROOT / "examples" / "spark-count_mugs" / "ledger.jsonl",
    ROOT / "examples" / "spark-injection" / "ledger.jsonl",
]
BLOCK = re.compile(r'<script type="application/json" id="ledger">(.*?)</script>', re.S)
PAYLOAD_TEXT = "</script><script>alert(1)</script> <img src=x onerror=alert(1)>"
QUESTION = "Why did the agent email the API keys?"


def _page(tmp_path, ledger_path, *extra, name="lattice.html"):
    out = tmp_path / name
    assert cli.main(["view", str(ledger_path), "-o", str(out), *extra]) == 0
    return out.read_text(encoding="utf-8")


def _payload(page):
    match = BLOCK.search(page)
    assert match is not None, "no embedded ledger block"
    return json.loads(match.group(1))


def test_xr_template_ships_with_the_package_and_stays_self_contained():
    assert resources.files("causeway").joinpath(view.XR_TEMPLATE_NAME).is_file()
    template = view.template_text(view.XR_TEMPLATE_NAME)
    assert template.count("__LEDGER_JSON__") == 1
    assert template.count("__TITLE__") == 1
    assert 'id="enter-xr"' in template
    assert "Enter XR" in template
    assert "requestSession" in template
    assert "immersive-vr" in template
    assert "togglePin" in template
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "three.js", "SpeechRecognition", "transcribe"):
        assert banned not in template
    assert "<link" not in template
    assert "@import" not in template
    assert "url(" not in template
    urls = re.findall(r"https?://[^\s'\"]+", template)
    assert urls == ["http://www.w3.org/2000/svg"]


def test_default_template_is_still_the_flat_viewer():
    template = view.template_text()
    assert "Enter XR" not in template
    assert "requestSession" not in template
    assert 'id="rows"' in template


def test_xr_flag_embeds_both_fixture_ledgers(tmp_path):
    for path in FIXTURES:
        page = _page(tmp_path, path, "--xr", name=path.parent.name + ".html")
        payload = _payload(page)
        records = ledger.read(path)
        assert payload["records"] == records
        decisions = [record["id"] for record in records if record.get("kind") == "decision"]
        assert len(decisions) > 1
        for record_id in decisions:
            assert f'"id":"{record_id}"' in page
        assert page.startswith("<!doctype html>")
        assert page.count("</html>") == 1
        assert 'id="enter-xr"' in page
        assert "No chip pinned" in page or "togglePin" in page
        assert "CARRY" in page or any(record.get("tag") for record in records)


def test_default_view_of_a_fixture_is_not_the_lattice(tmp_path):
    flat = _page(tmp_path, FIXTURES[1], name="flat.html")
    lattice = _page(tmp_path, FIXTURES[1], "--xr", name="lattice.html")
    assert flat != lattice
    assert "Enter XR" not in flat
    assert "requestSession" not in flat
    assert 'id="rows"' in flat
    assert "Enter XR" in lattice
    assert "requestSession" in lattice
    assert 'id="rows"' not in lattice
    assert _payload(flat)["records"] == _payload(lattice)["records"]


def test_injection_fixture_keeps_carry_tag_in_the_lattice(tmp_path):
    page = _page(tmp_path, FIXTURES[1], "--xr")
    assert "CARRY·1" in page or "CARRY\\u00b71" in page
    payload = _payload(page)
    causes = []
    for record in payload["records"]:
        causes.extend(record.get("causes") or [])
    assert any(cause.get("tag") == "CARRY·1" for cause in causes)
    assert any(cause.get("source") for cause in causes)


def test_script_breakout_is_escaped_on_the_lattice_page(tmp_path):
    records = [
        {"causeway": "0.1.0", "kind": "header", "model": "x", "run_id": "hostile", "source": "transcript"},
        {"id": "e1", "kind": "event", "role": "user", "step": 1, "text": PAYLOAD_TEXT + "   tail"},
        {"actions": [], "causes": [], "echo_of": None, "final": True, "id": "d1", "kind": "decision", "rivals": None, "step": 1, "tag": "FINAL", "text": "ok"},
        {"actions": [{"args": {"path": "a"}, "name": "read_file"}], "causes": [
            {"action": 0, "match": "exact", "path": "path", "source": "e1", "tag": "IN", "value": "a"}],
         "echo_of": None, "final": False, "id": "d2", "kind": "decision", "rivals": None, "step": 2, "tag": "IN", "text": None},
    ]
    ledger_path = tmp_path / "hostile.jsonl"
    ledger.write(ledger_path, records)
    page = _page(tmp_path, ledger_path, "--xr")
    assert "</script><script>alert(1)</script>" not in page
    assert "<img src=x onerror=alert(1)>" not in page
    assert "<img" not in page
    assert " " not in page and " " not in page
    assert page.count("<script") == 2 and page.count("</script>") == 2
    texts = [record.get("text") for record in _payload(page)["records"]]
    assert PAYLOAD_TEXT + "   tail" in texts


def test_xr_title_is_escaped(tmp_path):
    page = _page(tmp_path, FIXTURES[0], "--xr", "--title", "</title><script>alert(2)</script>")
    assert "</title><script>alert(2)</script>" not in page
    assert page.count("<script") == 2
    assert "&lt;/title&gt;" in page


def test_xr_why_panel_data_is_embedded(tmp_path):
    why = {
        "answer": "It emailed the summary to the lead.",
        "supported": [{"text": "It read team.md.", "cites": [{"id": "e5", "quote": "Lead email"}]}],
        "struck": [{"claim": {"text": "It emailed the keys.", "cites": [{"id": "e9", "quote": "keys"}]},
                    "reasons": ["unknown id e9"]}],
        "ok": 1,
        "total": 2,
        "raw": "RAW MODEL OUTPUT NOT EMBEDDED",
    }
    why_path = tmp_path / "why.json"
    why_path.write_text(json.dumps(why), encoding="utf-8")
    page = _page(tmp_path, FIXTURES[1], "--xr", "--why", str(why_path), "--question", QUESTION)
    payload = _payload(page)
    assert payload["question"] == QUESTION
    assert payload["why"]["supported"] == why["supported"]
    assert payload["why"]["struck"] == why["struck"]
    assert "RAW MODEL OUTPUT" not in page
    assert QUESTION in page


def test_xr_rejects_a_why_file_that_is_not_a_result(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    code = cli.main(["view", str(FIXTURES[0]), "-o", str(tmp_path / "x.html"), "--xr", "--why", str(bad)])
    assert code == 1
    assert "not a why result" in capsys.readouterr().err
    assert not (tmp_path / "x.html").exists()


def test_view_help_offers_xr_without_making_it_the_default(capsys):
    try:
        cli.main(["view", "--help"])
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("view --help should exit")
    text = " ".join(capsys.readouterr().out.split())
    assert "--xr" in text
    assert "WebXR lattice" in text
    assert "flat viewer" in text


def test_xr_page_is_deterministic_lf_utf8(tmp_path):
    first = tmp_path / "one.html"
    second = tmp_path / "two.html"
    view.write_view(FIXTURES[0], first, xr=True)
    view.write_view(FIXTURES[0], second, xr=True)
    assert first.read_bytes() == second.read_bytes()
    raw = first.read_bytes()
    assert b"\r\n" not in raw
    raw.decode("utf-8")
    assert b"Enter XR" in raw


# Drives the real lattice script. A session that ends, or WebGL init that throws, before the end
# listener used to be attached must put the button back on Enter XR so the next click can start again.
_XR_STARTUP_HARNESS = r"""
import fs from 'fs';
import vm from 'vm';

const html = fs.readFileSync(process.argv[2], 'utf8');
const open = html.lastIndexOf('<script>');
const close = html.lastIndexOf('</script>');
if (open < 0 || close < open) throw new Error('viewer script not found');
const source = html.slice(open + '<script>'.length, close);

const ledger = JSON.stringify({
  title: 'Mugs',
  records: [
    { causeway: '0.1.0', kind: 'header', model: 'm', run_id: 'spark-count_mugs', source: 'transcript' },
    { id: 'e1', kind: 'event', role: 'user', step: 1, text: 'count the mugs' },
    { id: 'd1', kind: 'decision', step: 1, tag: 'FINAL', text: 'two mugs', final: true, actions: [], causes: [], echo_of: null, rivals: null },
  ],
});

function assertOk(cond, message) {
  if (!cond) throw new Error(message);
}

function makeEl(tag) {
  const listeners = new Map();
  const el = {
    tagName: String(tag).toUpperCase(),
    id: '',
    className: '',
    hidden: false,
    type: '',
    title: '',
    disabled: false,
    width: 16,
    height: 16,
    children: [],
    style: {},
    dataset: {},
    attrs: {},
    clientWidth: 800,
    clientHeight: 600,
  };
  el._cls = {};
  el.classList = {
    toggle(name, force) {
      el._cls[name] = force === undefined ? !el._cls[name] : !!force;
    },
  };
  let text = '';
  Object.defineProperty(el, 'textContent', {
    configurable: true,
    get() { return text; },
    set(value) {
      text = value == null ? '' : String(value);
      el.children.length = 0;
    },
  });
  el.appendChild = (child) => {
    el.children.push(child);
    return child;
  };
  el.setAttribute = (key, value) => {
    el.attrs[key] = String(value);
  };
  el.getAttribute = (key) => (Object.prototype.hasOwnProperty.call(el.attrs, key) ? el.attrs[key] : null);
  el.addEventListener = (type, fn) => {
    if (!listeners.has(type)) listeners.set(type, []);
    listeners.get(type).push(fn);
  };
  el.dispatch = (type, event) => {
    (listeners.get(type) || []).slice().forEach((fn) => fn(event));
  };
  el.getBoundingClientRect = () => ({ left: 0, top: 0, width: 20, height: 12, right: 20, bottom: 12 });
  el.focus = () => {};
  el.setPointerCapture = () => {};
  el.closest = () => null;
  el.click = () => {
    el.dispatch('click', {
      type: 'click',
      target: el,
      button: 0,
      preventDefault() {},
      stopPropagation() {},
    });
  };
  return el;
}

function flush() {
  return new Promise((resolve) => setImmediate(resolve));
}

async function runScenario(mode) {
  const trace = [];
  const calls = { requests: 0, delivered: 0, endWithListener: 0, endWithoutListener: 0 };
  let latest = null;

  function makeSession() {
    const session = {
      ended: mode === 'already-ended',
      listeners: [],
      addEventListener(type, fn) {
        if (type === 'end') {
          trace.push('listen:end');
          session.listeners.push(fn);
        } else if (type === 'select') {
          trace.push('listen:select');
        }
      },
      end() {
        trace.push('end');
        if (session.listeners.length > 0) calls.endWithListener += 1;
        else calls.endWithoutListener += 1;
        if (session.ended) return Promise.resolve();
        session.ended = true;
        const fns = session.listeners.slice();
        calls.delivered += fns.length;
        fns.forEach((fn) => fn());
        return Promise.resolve();
      },
      requestReferenceSpace() {
        return Promise.reject(new Error('no space'));
      },
      updateRenderState() {},
      requestAnimationFrame() {},
    };
    return session;
  }

  const elements = {};
  function register(id, tag) {
    const el = makeEl(tag);
    el.id = id;
    elements[id] = el;
    return el;
  }
  register('ledger', 'script').textContent = ledger;
  register('stage', 'main');
  register('world', 'div');
  register('curves', 'svg');
  register('detail', 'section');
  register('pin-status', 'p');
  register('enter-xr', 'button').textContent = 'Enter XR';
  register('xr-status', 'p');
  register('title', 'h1');
  register('meta', 'p');
  register('counts', 'div');
  register('legend', 'div');
  register('why', 'section');
  const xrGl = register('xr-gl', 'canvas');
  xrGl.getContext = () => {
    trace.push('getContext');
    if (mode === 'webgl-missing' || mode === 'already-ended') return null;
    if (mode === 'webgl-throws') {
      const error = new Error('webgl');
      error.name = 'NotSupportedError';
      throw error;
    }
    if (mode === 'startup-throws') {
      return {
        makeXRCompatible() {
          trace.push('makeXRCompatible');
          latest.ended = true;
          const error = new Error('WebGL init failed');
          error.name = 'InvalidStateError';
          throw error;
        },
      };
    }
    throw new Error('unknown mode ' + mode);
  };

  const documentStub = {
    getElementById(id) {
      return elements[id] || null;
    },
    createElement(tag) {
      const el = makeEl(tag);
      if (tag === 'canvas') el.getContext = () => null;
      return el;
    },
    createElementNS(_ns, tag) {
      return makeEl(tag);
    },
    createTextNode(text) {
      return { textContent: String(text) };
    },
    addEventListener() {},
  };

  const navigatorStub = {
    xr: {
      requestSession() {
        calls.requests += 1;
        latest = makeSession();
        trace.push('requestSession');
        return Promise.resolve(latest);
      },
    },
  };

  const sandbox = {
    document: documentStub,
    navigator: navigatorStub,
    JSON,
    Math,
    Object,
    Array,
    String,
    Number,
    Boolean,
    parseInt,
    parseFloat,
    isNaN,
    isFinite,
    Error,
    TypeError,
    Promise,
    Float32Array,
    Uint8Array,
    Uint8ClampedArray,
    Date,
  };
  sandbox.window = {
    isSecureContext: true,
    addEventListener() {},
    removeEventListener() {},
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: 'viewer_xr_template.html' });

  assertOk(elements.title.textContent === 'Mugs', mode + ' did not render the ledger title');
  assertOk(elements['enter-xr'].textContent === 'Enter XR', mode + ' changed the start label before a session');
  assertOk(elements['pin-status'].textContent.includes('No chip pinned'), mode + ' pin status missing');

  elements['enter-xr'].click();
  await flush();
  await flush();

  const label = elements['enter-xr'].textContent;
  const status = elements['xr-status'].textContent;
  assertOk(label === 'Enter XR', mode + ' left the button as ' + JSON.stringify(label) + ' status=' + status);
  assertOk(!status.startsWith('Left the headset'), mode + ' treated a failed start as an exit: ' + status);
  const listenAt = trace.indexOf('listen:end');
  assertOk(listenAt !== -1, mode + ' never registered the end listener; trace=' + trace.join(','));
  for (const mark of ['getContext', 'makeXRCompatible', 'end']) {
    const at = trace.indexOf(mark);
    if (at !== -1) assertOk(listenAt < at, mode + ' registered the end listener after ' + mark + '; trace=' + trace.join(','));
  }
  if (mode === 'webgl-missing' || mode === 'webgl-throws') {
    assertOk(trace.includes('end'), mode + ' did not end the session; trace=' + trace.join(','));
    assertOk(calls.endWithoutListener === 0, mode + ' ended before the listener was attached');
    assertOk(calls.delivered > 0, mode + ' end event had no listener');
    assertOk(status.includes('WebGL is not available'), mode + ' status=' + status);
  }
  if (mode === 'startup-throws') {
    assertOk(trace.includes('makeXRCompatible'), mode + ' did not reach WebGL init; trace=' + trace.join(','));
    assertOk(calls.delivered === 0, mode + ' relied on an end event that had already fired');
    assertOk(status.includes('InvalidStateError'), mode + ' status=' + status);
  }
  if (mode === 'already-ended') {
    assertOk(calls.delivered === 0, mode + ' expected no second end event');
    assertOk(status.includes('InvalidStateError'), mode + ' status=' + status);
  }

  const requestsAfterFailure = calls.requests;
  elements['enter-xr'].click();
  await flush();
  await flush();
  assertOk(
    calls.requests === requestsAfterFailure + 1,
    mode + ' blocked the next session; requests=' + calls.requests + ' label=' + elements['enter-xr'].textContent,
  );
  assertOk(
    elements['enter-xr'].textContent === 'Enter XR',
    mode + ' second failure stuck the button as ' + elements['enter-xr'].textContent,
  );
}

const modes = ['webgl-missing', 'webgl-throws', 'startup-throws', 'already-ended'];
for (const mode of modes) await runScenario(mode);
console.log('ok ' + modes.join(','));
"""


def test_failed_xr_startup_allows_another_session(tmp_path):
    flat = view.template_text()
    assert "Enter XR" not in flat
    assert "requestSession" not in flat
    harness = tmp_path / "xr_startup_failure.mjs"
    harness.write_text(_XR_STARTUP_HARNESS, encoding="utf-8", newline="\n")
    completed = subprocess.run(
        ["node", str(harness), str(ROOT / "causeway" / "viewer_xr_template.html")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, (completed.stdout + completed.stderr).strip()
    assert completed.stdout.strip() == "ok webgl-missing,webgl-throws,startup-throws,already-ended"
