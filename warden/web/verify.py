"""Public receipt verification: anyone can check an Intent Receipt without trusting Warden.

Checks shown per receipt:
  1. Ed25519 signature against Warden's published public key
  2. receipt hash (the body has not been edited)
  3. chain link to the previous receipt (none removed or reordered)
  4. the audit-log entry it was issued from exists, and the audit chain is intact
  5. Alta (formerly Metal) receipts only: the payment is re-verified on chain against the human-signed mandate

Mount with `app.include_router(create_verify_router())`. The page shows no secrets:
receipts only carry hashed or masked identifiers.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from warden import receipts as rc
from warden.audit import AuditLog
from warden.config import ROOT, load_settings
from warden.rails.cli import CliRunner

METAL_CLI = ROOT / "tools" / "metal" / "node_modules" / ".bin" / "metal-agent"
METAL_ISSUER = ROOT / "secrets" / "metal-owner-key.issuer.public.json"
ONCHAIN_CACHE = ROOT / "data" / "onchain_cache.json"
_STEP = re.compile(r"^step\s+(\d+)\s+(pass|fail|skipped):\s*(.*)$", re.MULTILINE)
_VERIFIED = re.compile(r"^verified\s+(yes|no)", re.MULTILINE)
_TX = re.compile(r"^0x[0-9a-fA-F]{64}$")


# --- on-chain check -------------------------------------------------------------------


@dataclass(frozen=True)
class OnchainResult:
    verified: bool
    steps: list[dict[str, str]]
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"verified": self.verified, "steps": self.steps, "detail": self.detail}


def parse_verify_output(output: str) -> OnchainResult:
    steps = [{"step": n, "result": r, "text": t.strip()} for n, r, t in _STEP.findall(output)]
    verdict = _VERIFIED.search(output)
    return OnchainResult(verified=bool(verdict and verdict.group(1) == "yes"), steps=steps,
                         detail="" if verdict else "metal-agent printed no verdict")


class OnchainVerifier:
    """Runs `metal-agent mandate verify` for one payment, reading only the chain.

    The credential is the receipt's own `metal_credential_ref` when present. Older receipts
    predate that field, so their candidates are tried newest first; the credential that
    verifies is cached per transaction (secrets/mandate.json is rewritten by demo resets).
    """

    def __init__(self, runner: Callable[..., Any] | None = None, secrets_dir: Path = ROOT / "secrets",
                 cache_path: Path = ONCHAIN_CACHE, issuer: Path = METAL_ISSUER) -> None:
        self._runner = runner
        self.secrets_dir = secrets_dir
        self.cache_path = cache_path
        self.issuer = issuer

    @property
    def runner(self) -> Callable[..., Any]:
        if self._runner is None:
            settings = load_settings()
            self._runner = CliRunner([str(METAL_CLI)], env={"METAL_RPC_URL": settings.metal_rpc_url}, timeout=90)
        return self._runner

    def check(self, tx: str, credential_ref: str | None) -> OnchainResult:
        if not _TX.match(tx or ""):
            return OnchainResult(False, [], "not a Alta transaction hash")
        cache = self._load_cache()
        if tx in cache:
            return OnchainResult(**cache[tx]["result"])
        last = OnchainResult(False, [], "no mandate credential found for this payment")
        for credential in self._candidates(credential_ref):
            result = self.runner("mandate", "verify", "--credential", str(credential), "--issuer", str(self.issuer),
                                 "--network", "testnet", "--tx", tx)
            last = parse_verify_output((result.stdout or "") + (result.stderr or ""))
            if last.verified:
                cache[tx] = {"credential": credential.name, "result": last.to_json()}
                self._save_cache(cache)
                break
        return last

    def _candidates(self, ref: str | None) -> list[Path]:
        if ref and (self.secrets_dir / ref).exists():
            return [self.secrets_dir / ref]
        files = [p for p in self.secrets_dir.glob("mandate*.json") if not p.name.endswith(".key.json")]
        return sorted(files, key=lambda p: (p.name == "mandate.json", -p.stat().st_mtime))

    def _load_cache(self) -> dict[str, Any]:
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_cache(self, cache: dict[str, Any]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(cache, indent=1), encoding="utf-8")


# --- checks ---------------------------------------------------------------------------


def check_receipt(receipt_id: str, receipts_path: Path, public_key_path: Path,
                  audit_path: Path | None) -> tuple[rc.Receipt, list[dict[str, Any]]]:
    receipts = rc.load(receipts_path)
    index = next((i for i, r in enumerate(receipts) if r.id == receipt_id), None)
    if index is None:
        raise KeyError(receipt_id)
    receipt = receipts[index]
    prev = receipts[index - 1].receipt_hash if index else rc.GENESIS
    result = rc.verify(receipt, rc.load_public_key(public_key_path), prev)
    checks = [
        _check("Signature", result.signature_ok, "Signed by Warden's published Ed25519 key."
               if result.signature_ok else "Signature does not match the published key."),
        _check("Receipt hash", result.hash_ok, "The receipt body is exactly as issued."
               if result.hash_ok else "The receipt body was edited after signing."),
        _check("Chain link", bool(result.chain_ok), f"Follows receipt #{index} in an unbroken chain."
               if result.chain_ok else "The previous receipt was removed, edited or reordered."),
    ]
    if audit_path is not None and audit_path.exists():
        log = AuditLog(audit_path)
        entry_hash = receipt.body.get("audit", {}).get("entry_hash")
        present = any(e.get("hash") == entry_hash for e in log.entries())
        intact = log.verify() is None
        checks.append(_check("Audit log", present and intact,
                             "Issued from an audit entry that is still in an intact hash chain." if present and intact
                             else "The audit entry is missing or the audit chain is broken."))
    return receipt, checks


def _check(name: str, ok: bool, text: str) -> dict[str, Any]:
    return {"name": name, "ok": ok, "text": text}


def check_pasted(data: dict[str, Any], public_key_path: Path) -> list[dict[str, Any]]:
    """Verify a receipt someone pasted: signature and hash only, since its neighbours are unknown."""
    receipt = rc.Receipt.from_json(data)
    result = rc.verify(receipt, rc.load_public_key(public_key_path))
    return [_check("Signature", result.signature_ok, "Valid Warden signature." if result.signature_ok
                   else "Signature does not match the published key."),
            _check("Receipt hash", result.hash_ok, "Body matches its hash." if result.hash_ok
                   else "The body does not match its hash.")]


# --- router ---------------------------------------------------------------------------


def create_verify_router(receipts_path: Path = rc.RECEIPTS_PATH, public_key_path: Path = rc.PUBLIC_KEY_PATH,
                         audit_path: Path | None = None, onchain: OnchainVerifier | None = None,
                         current: Callable[[], tuple[Path, Path, Path]] | None = None) -> APIRouter:
    """`current`, when given, is called before every request: it issues any receipts not yet written and
    returns the paths in use right now, so the page is never behind the console (or a reset)."""
    router = APIRouter()
    onchain = onchain or OnchainVerifier()
    if audit_path is None and current is None:
        audit_path = load_settings().audit_path

    def paths() -> tuple[Path, Path, Path]:
        return current() if current else (receipts_path, public_key_path, audit_path)

    @router.get("/verify", response_class=HTMLResponse)
    def index() -> str:
        receipts_file, key_file, _ = paths()
        receipts = rc.load(receipts_file)
        pem = key_file.read_text(encoding="ascii") if key_file.exists() else ""
        rows = "".join(_receipt_row(r) for r in reversed(receipts[-25:])) or \
            ('<p class="muted">No receipts issued yet. In the offline demo, run something in the '
             '<a href="/console">console</a> first. The published live run can be checked with nothing but '
             'its public key:</p><pre>python -m warden.receipts verify docs/live-runs/2026-11-13/receipts.jsonl '
             'docs/live-runs/2026-11-13/receipt-public-key.pem</pre>')
        return _page("Verify an Intent Receipt", f"""
          <section class="hero">
            <p class="eyebrow">Public verification</p>
            <h1>Don't trust Warden. <em>Check it.</em></h1>
            <p class="lede">Every payment decision Warden makes becomes an Intent Receipt: what the agent asked for, what the
              human allowed, what Warden decided and what the rail did. Anyone can check one, with no account and no
              trust in Warden.</p>
            <div class="steps">
              <div class="step"><span>1</span><b>Signature</b><p>Signed with Warden's Ed25519 key, published below.
                Change one character and it fails.</p></div>
              <div class="step"><span>2</span><b>Chain</b><p>Each receipt carries the hash of the one before. Delete or
                rewrite one and every later link breaks.</p></div>
              <div class="step"><span>3</span><b>On chain</b><p>For Alta payments, the payment itself is re-checked on
                the testnet against the mandate a human signed.</p></div>
            </div>
          </section>
          <div class="cols">
          <div class="col-main">
          <section class="panel"><h2>Recent receipts <span class="count">{len(receipts)}</span></h2><div class="rows">{rows}</div></section>
          </div>
          <div class="col-side">
          <section class="panel">
            <h2>Look up a receipt</h2>
            <form class="lookup" onsubmit="location.href='/verify/'+encodeURIComponent(this.r.value.trim());return false">
              <input name="r" placeholder="rcpt_…" autocomplete="off"><button class="btn">Verify</button>
            </form>
            <details><summary>Or paste a receipt's JSON</summary>
              <textarea id="paste" rows="7" placeholder='{{"receipt_id": …}}'></textarea>
              <button class="btn" onclick="checkPaste()">Check signature</button><div id="paste-out"></div>
            </details>
          </section>
          <section class="panel"><h2>Warden's public key</h2>
            <p class="muted">Every receipt is signed with the matching private key. Check signatures yourself with any
            Ed25519 library over the receipt's canonical JSON (sorted keys, no spaces), excluding
            <code>receipt_hash</code> and <code>signature</code>.</p><pre>{html.escape(pem)}</pre>
            <p class="muted small">Or offline: <code>python -m warden.receipts verify receipts.jsonl receipt-public-key.pem</code></p></section>
          </div></div>
          <script>
          async function checkPaste() {{
            const out = document.getElementById('paste-out');
            try {{
              const res = await fetch('/verify/paste', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
                body: document.getElementById('paste').value}});
              const data = await res.json();
              out.innerHTML = (data.checks || []).map(c => `<div class="check ${{c.ok ? 'ok' : 'bad'}}">
                <span>${{c.ok ? '✓' : '✗'}}</span><b>${{c.name}}</b> ${{c.text}}</div>`).join('') || data.detail;
            }} catch (e) {{ out.textContent = 'Not valid JSON.'; }}
          }}
          </script>""")

    @router.post("/verify/paste")
    async def paste(request: Request) -> JSONResponse:
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError
            return JSONResponse({"checks": check_pasted(data, paths()[1])})
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"detail": "That is not a Warden receipt."}, status_code=400)

    @router.get("/verify/{receipt_id}/json")
    def receipt_json(receipt_id: str) -> JSONResponse:
        receipt = _find(receipt_id)
        return JSONResponse(receipt.to_json())

    @router.get("/verify/{receipt_id}/onchain")
    def receipt_onchain(receipt_id: str) -> JSONResponse:
        receipt = _find(receipt_id)
        tx = (receipt.body.get("execution") or {}).get("transaction")
        if not tx:
            raise HTTPException(404, "This receipt has no Alta transaction.")
        return JSONResponse(onchain.check(tx, receipt.body.get("metal_credential_ref")).to_json())

    @router.get("/verify/{receipt_id}", response_class=HTMLResponse)
    def receipt_page(receipt_id: str) -> str:
        try:
            receipt, checks = check_receipt(receipt_id, *paths())
        except KeyError:
            raise HTTPException(404, "No receipt with that id.") from None
        return _page(f"Receipt {receipt.id}", _receipt_detail(receipt, checks))

    def _find(receipt_id: str) -> rc.Receipt:
        for r in rc.load(paths()[0]):
            if r.id == receipt_id:
                return r
        raise HTTPException(404, "No receipt with that id.")

    return router


# --- rendering ------------------------------------------------------------------------

_BADGE = {"ALLOW": "allow", "BLOCK": "block", "CONTAIN": "contain"}


def _receipt_row(r: rc.Receipt) -> str:
    b = r.body
    action = (b.get("decision") or {}).get("action", "")
    return (f'<a class="row" href="/verify/{html.escape(r.id)}"><span class="badge {_BADGE.get(action, "")}">'
            f'{html.escape(action)}</span><span class="who"><b>{html.escape(str((b.get("payee") or {}).get("name") or ""))}</b>'
            f'<span class="mono muted">{html.escape(r.id)} · {html.escape(str(b.get("agent_id") or ""))}</span></span>'
            f'<span class="amt"><b>{html.escape(str(b.get("amount") or ""))} {html.escape(str(b.get("currency") or ""))}</b>'
            f'<span class="muted">{html.escape(str(b.get("settlement_status") or ""))}</span></span></a>')


def _receipt_detail(receipt: rc.Receipt, checks: list[dict[str, Any]]) -> str:
    b = receipt.body
    d = b.get("decision") or {}
    e = b.get("execution") or {}
    all_ok = all(c["ok"] for c in checks)
    check_html = "".join(f'<div class="check {"ok" if c["ok"] else "bad"}"><span>{"✓" if c["ok"] else "✗"}</span>'
                         f'<b>{html.escape(c["name"])}</b> {html.escape(c["text"])}</div>' for c in checks)
    onchain = ""
    if e.get("transaction"):
        onchain = f"""<div class="check pending" id="onchain"><span>…</span><b>On chain</b>
          <button class="btn btn-sm" onclick="onchainCheck()">Verify on Alta testnet</button></div>
          <script>
          async function onchainCheck() {{
            const box = document.getElementById('onchain');
            box.innerHTML = '<span>…</span><b>On chain</b> Reading the Alta testnet…';
            const res = await fetch('/verify/{html.escape(receipt.id)}/onchain');
            const data = await res.json();
            box.className = 'check ' + (data.verified ? 'ok' : 'bad');
            box.innerHTML = `<span>${{data.verified ? '✓' : '✗'}}</span><b>On chain</b> ` + (data.verified
              ? 'Paid under a mandate the account owner signed and bound on chain.'
              : 'Not verified. ' + (data.detail || '')) +
              '<ul>' + (data.steps || []).map(s => `<li>${{s.result === 'pass' ? '✓' : s.result === 'fail' ? '✗' : '–'}}
                step ${{s.step}}: ${{s.text}}</li>`).join('') + '</ul>';
          }}
          </script>"""
    risk = d.get("risk") or {}
    fields = [
        ("Decision", f'<span class="badge {_BADGE.get(d.get("action", ""), "")}">{html.escape(str(d.get("action")))}</span>'),
        ("Amount", f'{html.escape(str(b.get("amount")))} {html.escape(str(b.get("currency")))}'),
        ("Payee", f'{html.escape(str((b.get("payee") or {}).get("name")))} '
                  f'<span class="mono muted">{html.escape(str((b.get("payee") or {}).get("id")))}</span>'),
        ("Rail", html.escape(f'{b.get("rail")} via {e.get("rail") or "—"}')),
        ("Agent", html.escape(str(b.get("agent_id")))),
        ("Settlement", html.escape(str(b.get("settlement_status")))),
        ("Reasons", html.escape("; ".join(d.get("reasons") or [])) or "—"),
        ("Violations", html.escape(", ".join(d.get("violations") or [])) or "none"),
        ("Risk read", html.escape(f'severity {risk.get("severity")} · {", ".join(risk.get("flags") or []) or "no flags"}'
                                  f' · {risk.get("source")}')),
        ("Why", html.escape(str(risk.get("explanation") or "—"))),
        ("Approval", html.escape(f'{b["approval"].get("approver")} · {b["approval"].get("approval_hash")}')
            if (b.get("approval") or {}).get("approval_hash") else "No human approval needed"),
    ]
    ids = [(k, v) for k, v in [
        ("Transfer", e.get("transfer_id")), ("Card transaction", e.get("card_transaction_id")),
        ("Alta transaction", e.get("transaction")), ("Airi mandate", e.get("airi_mandate_id")),
        ("Mandate hash", b.get("mandate_hash")), ("Evidence hash", b.get("evidence_hash")),
        ("Receipt hash", receipt.receipt_hash), ("Previous receipt", b.get("prev_receipt_hash")),
        ("Audit entry", (b.get("audit") or {}).get("entry_hash"))] if v]
    id_html = "".join(f'<div class="kv"><span>{html.escape(k)}</span><span class="mono">'
                      + (f'<a href="{html.escape(e["explorer"])}" target="_blank" rel="noopener">{html.escape(str(v))}</a>'
                         if k == "Alta transaction" and e.get("explorer") else html.escape(str(v)))
                      + "</span></div>" for k, v in ids)
    field_html = "".join(f'<div class="kv"><span>{k}</span><span>{v}</span></div>' for k, v in fields)
    amount = f'{html.escape(str(b.get("amount")))} {html.escape(str(b.get("currency")))}'
    payee = html.escape(str((b.get("payee") or {}).get("name")))
    return f"""
      <p class="crumb"><a href="/verify">← All receipts</a></p>
      <div class="cols detail">
      <div class="col-side sticky">
      <section class="panel verdict {'ok' if all_ok else 'bad'}">
        <div class="seal" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M12 2 4 5v6c0 5 3.4 9.4 8 11 4.6-1.6 8-6 8-11V5z"/>
          <path class="mark" d="{'m8.5 12 2.5 2.5 4.5-5' if all_ok else 'm9 9 6 6m0-6-6 6'}"/></svg></div>
        <h2>{'✓ Authentic receipt' if all_ok else '✗ This receipt does not verify'}</h2>
        <p class="sum">{html.escape(str(d.get("action")))} · {amount} · {payee}</p>
        <p class="muted mono">{html.escape(receipt.id)} · issued {html.escape(str(b.get("issued_at")))}
          · signer {html.escape(str(b.get("signer")))}</p>
        <div class="checks">{check_html}{onchain}</div>
      </section>
      </div>
      <div class="col-main">
      <section class="panel"><h2>What was authorised, and what happened</h2>{field_html}</section>
      <section class="panel"><h2>Bound identifiers</h2>{id_html}
        <p><a href="/verify/{html.escape(receipt.id)}/json">Download receipt JSON</a> · <a href="/verify">All receipts</a></p>
      </section>
      </div></div>"""


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)} · Warden</title>
<script src="/static/theme.js?v=2"></script><link rel="stylesheet" href="/static/theme-toggle.css?v=2">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Instrument+Serif&family=JetBrains+Mono:wght@400&display=swap" rel="stylesheet">
<style>{_CSS}</style></head><body>
<header class="bar"><div class="top"><a href="/verify" class="brand"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2 4 5v6c0 5 3.4 9.4 8 11 4.6-1.6 8-6 8-11V5z"/><path class="tick" d="m8.5 12 2.5 2.5 4.5-5"/></svg>Warden <span class="pill">Verify</span></a>
<nav><a href="/lab">Lab</a><a href="/">Story</a><a href="/console">Console</a><button class="theme-toggle" type="button" aria-label="Switch theme">
<span class="tt-opt tt-dark"><svg viewBox="0 0 24 24"><path d="M20.5 13.2A8.5 8.5 0 1 1 10.8 3.5a6.6 6.6 0 0 0 9.7 9.7z"/></svg></span>
<span class="tt-opt tt-light"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6 6 6M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4"/></svg></span>
</button></nav></div></header>
<main>{body}</main></body></html>"""


_CSS = """
:root { --bg:#050607; --surface:#0d1013; --surface-2:#15191e; --border:rgba(243,239,230,.1); --border-2:rgba(243,239,230,.18);
  --text:#f3efe6; --text-2:#c9c4b8; --muted:#8f8b82; --green:#2bd4a4; --green-deep:#0f6e56; --green-bg:rgba(43,212,164,.12);
  --amber:#ffb547; --red:#ff5a47; --red-bg:rgba(255,90,71,.12); --crimson:#ff3d71; --crimson-bg:rgba(255,61,113,.14);
  --blue:#8ab4ff; --mono:"JetBrains Mono",ui-monospace,Consolas,monospace; --serif:"Instrument Serif",Georgia,serif; color-scheme:dark }
:root[data-theme="light"] { --bg:#f7f5f0; --surface:#ffffff; --surface-2:#f3f0e8; --border:#e7e2d8; --border-2:#d9d3c6;
  --text:#15171c; --text-2:#3a3f48; --muted:#6c7079; --green:#13815e; --green-bg:#e5f3ec; --amber:#b3741a; --red:#c2392c;
  --red-bg:#fbebe7; --crimson:#a3123a; --crimson-bg:#fce7ed; --blue:#2a56c4; color-scheme:light }
* { box-sizing:border-box } body { margin:0; background:var(--bg); color:var(--text);
  font:15.5px/1.55 Inter,system-ui,"Segoe UI",sans-serif; -webkit-font-smoothing:antialiased }
a { color:var(--blue) } code { font-family:var(--mono); font-size:12.5px }
.bar { position:sticky; top:0; z-index:5; background:color-mix(in srgb, var(--bg) 88%, transparent); backdrop-filter:blur(10px);
  border-bottom:1px solid var(--border) }
.bar .top { max-width:1240px; margin:0 auto; padding:14px clamp(16px,4vw,40px); display:flex; justify-content:space-between;
  align-items:center; gap:16px; flex-wrap:wrap }
.brand { display:flex; align-items:center; gap:10px; color:var(--text); text-decoration:none; font-family:var(--serif); font-size:26px }
.brand svg { width:24px; height:24px; fill:none; stroke-linecap:round; stroke-linejoin:round }
.brand svg path:first-child { fill:var(--green-deep); stroke:var(--green); stroke-width:1.6 } .brand .tick { stroke:#fff; stroke-width:2.4 }
.pill { font:600 11px/1 Inter,sans-serif; letter-spacing:.16em; text-transform:uppercase; color:var(--green);
  border:1px solid var(--border-2); border-radius:999px; padding:5px 9px }
.bar nav { display:flex; align-items:center; gap:18px; font-size:14px } .bar nav a { color:var(--text-2); text-decoration:none }
.bar nav a:hover { color:var(--text) }
.theme-toggle { --tt-line:var(--border); --tt-bg:var(--surface); --tt-ink:var(--muted); --tt-knob:var(--surface-2); --tt-active:var(--green) }
main { max-width:1240px; margin:0 auto; padding:0 clamp(16px,4vw,40px) 72px }
.hero { padding:clamp(36px,6vw,72px) 0 28px }
.eyebrow { margin:0 0 10px; font-size:12.5px; font-weight:600; letter-spacing:.18em; text-transform:uppercase; color:var(--green) }
h1 { margin:0; font-family:var(--serif); font-weight:400; font-size:clamp(38px,5.4vw,72px); line-height:1.03; letter-spacing:-.02em }
h1 em { font-style:italic; color:var(--green) }
.lede { max-width:760px; color:var(--text-2); font-size:clamp(15.5px,1.25vw,18px); margin:18px 0 26px }
.steps { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px }
.step { border:1px solid var(--border); border-radius:14px; padding:16px 18px; background:var(--surface) }
.step span { display:inline-grid; place-items:center; width:26px; height:26px; border-radius:50%; background:var(--green-bg);
  color:var(--green); font:600 13px var(--mono); margin-right:8px } .step b { font-size:15px }
.step p { margin:8px 0 0; color:var(--muted); font-size:13.5px }
.cols { display:grid; grid-template-columns:minmax(0,1.65fr) minmax(0,1fr); gap:18px; align-items:start; margin-top:18px }
.cols.detail { grid-template-columns:minmax(0,1fr) minmax(0,1.25fr) }
.sticky { position:sticky; top:82px }
.crumb { margin:22px 0 0; font-size:14px } .crumb a { color:var(--text-2); text-decoration:none } .crumb a:hover { color:var(--green) }
h2 { font-size:15px; font-weight:600; margin:0 0 14px; display:flex; align-items:center; gap:8px }
.count { font:600 12px var(--mono); color:var(--muted); background:var(--surface-2); border-radius:999px; padding:2px 8px }
.panel { background:var(--surface); border:1px solid var(--border); border-radius:16px; padding:20px 22px; margin-bottom:18px }
.verdict { position:relative; overflow:hidden }
.verdict.ok { border-color:color-mix(in srgb, var(--green) 60%, transparent);
  box-shadow:0 0 0 1px color-mix(in srgb, var(--green) 20%, transparent), 0 40px 90px -60px var(--green) }
.verdict.bad { border-color:var(--red); box-shadow:0 40px 90px -60px var(--red) }
.verdict h2 { font-family:var(--serif); font-weight:400; font-size:32px; margin:12px 0 4px }
.verdict.ok h2 { color:var(--green) } .verdict.bad h2 { color:var(--red) }
.verdict .sum { margin:0 0 4px; font-weight:600; color:var(--text) }
.seal { width:64px; height:64px; border-radius:18px; display:grid; place-items:center; background:var(--green-bg) }
.verdict.bad .seal { background:var(--red-bg) }
.seal svg { width:38px; height:38px; fill:none; stroke-width:1.6; stroke-linecap:round; stroke-linejoin:round }
.verdict.ok .seal path:first-child { fill:var(--green-deep); stroke:var(--green) }
.verdict.bad .seal path:first-child { fill:#5a1a14; stroke:var(--red) }
.seal .mark { stroke:#fff; stroke-width:2.4 }
.checks { margin-top:14px }
.muted { color:var(--muted) } .small { font-size:13px } .mono { font-family:var(--mono); font-size:12.5px; word-break:break-all }
.check { display:grid; grid-template-columns:16px 112px minmax(0,1fr) auto; gap:4px 10px; align-items:baseline; padding:10px 12px; border-radius:10px;
  background:var(--surface-2); margin-bottom:7px; animation:rise .45s cubic-bezier(.2,.8,.2,1) both }
.check:nth-child(2) { animation-delay:.08s } .check:nth-child(3) { animation-delay:.16s }
.check:nth-child(4) { animation-delay:.24s } .check:nth-child(5) { animation-delay:.32s }
@keyframes rise { from { opacity:0; transform:translateY(6px) } to { opacity:1; transform:none } }
@media (prefers-reduced-motion: reduce) { .check { animation:none } }
.check > span:first-child { font-weight:700 } .check ul { grid-column:1 / -1 }
.check.ok > span:first-child { color:var(--green) } .check.bad > span:first-child { color:var(--red) }
.check ul { flex-basis:100%; margin:6px 0 0; color:var(--muted); font-size:13px; padding-left:18px }
.kv { display:grid; grid-template-columns:150px 1fr; gap:14px; padding:9px 0; border-bottom:1px solid var(--border) }
.kv:last-of-type { border-bottom:0 } .kv span:first-child { color:var(--muted) }
.badge { display:inline-block; min-width:72px; text-align:center; font:600 11.5px var(--mono); padding:3px 8px; border-radius:6px;
  background:var(--surface-2) }
.badge.allow { color:var(--green); background:var(--green-bg) } .badge.block { color:var(--red); background:var(--red-bg) }
.badge.contain { color:var(--crimson); background:var(--crimson-bg) }
.rows .row { display:grid; grid-template-columns:92px minmax(0,1fr) auto; gap:14px; padding:11px 10px;
  border-bottom:1px solid var(--border); color:var(--text); text-decoration:none; align-items:center; border-radius:10px }
.row .who, .row .amt { display:flex; flex-direction:column; min-width:0 } .row .amt { text-align:right }
.row .who b, .row .amt b { font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis }
.row .who .mono { font-size:11.5px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; word-break:normal }
.row .amt .muted { font:11.5px var(--mono) }
.rows .row:last-child { border-bottom:0 } .rows .row:hover { background:var(--surface-2) }
.btn { background:var(--surface-2); color:var(--text); border:1px solid var(--border-2); border-radius:10px;
  padding:9px 16px; font:600 14px Inter,sans-serif; cursor:pointer } .btn:hover { border-color:var(--green); color:var(--green) }
.btn-sm { padding:5px 12px; font-size:13px }
.lookup { display:flex; gap:8px; margin-bottom:10px } input, textarea { flex:1; width:100%; background:var(--bg);
  color:var(--text); border:1px solid var(--border-2); border-radius:10px; padding:9px 12px; font:13px var(--mono) }
summary { cursor:pointer; color:var(--text-2); font-size:14px }
textarea { margin:8px 0 }
pre { background:var(--bg); padding:12px; border-radius:10px; overflow:auto; font:11.5px var(--mono); border:1px solid var(--border) }
@media (max-width:1000px) { .cols, .cols.detail { grid-template-columns:1fr } .sticky { position:static } .steps { grid-template-columns:1fr } }
@media (max-width:640px) { .check { grid-template-columns:16px minmax(0,1fr) } .check b { grid-column:2 } .kv { grid-template-columns:1fr; gap:2px } .bar nav a { display:none } }
"""
