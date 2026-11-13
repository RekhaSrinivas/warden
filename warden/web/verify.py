"""Public receipt verification: anyone can check an Intent Receipt without trusting Warden.

Checks shown per receipt:
  1. Ed25519 signature against Warden's published public key
  2. receipt hash (the body has not been edited)
  3. chain link to the previous receipt (none removed or reordered)
  4. the audit-log entry it was issued from exists, and the audit chain is intact
  5. Metal receipts only: the payment is re-verified on chain against the human-signed mandate

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
            return OnchainResult(False, [], "not a Metal transaction hash")
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
                         audit_path: Path | None = None, onchain: OnchainVerifier | None = None) -> APIRouter:
    router = APIRouter()
    onchain = onchain or OnchainVerifier()
    if audit_path is None:
        audit_path = load_settings().audit_path

    @router.get("/verify", response_class=HTMLResponse)
    def index() -> str:
        receipts = rc.load(receipts_path)
        pem = public_key_path.read_text(encoding="ascii") if public_key_path.exists() else ""
        rows = "".join(_receipt_row(r) for r in reversed(receipts[-25:])) or \
            '<p class="muted">No receipts issued yet.</p>'
        return _page("Verify an Intent Receipt", f"""
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
          <section class="panel"><h2>Recent receipts</h2><div class="rows">{rows}</div></section>
          <section class="panel"><h2>Warden's public key</h2>
            <p class="muted">Every receipt is signed with the matching private key. Check signatures yourself with any
            Ed25519 library over the receipt's canonical JSON (sorted keys, no spaces), excluding
            <code>receipt_hash</code> and <code>signature</code>.</p><pre>{html.escape(pem)}</pre></section>
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
            return JSONResponse({"checks": check_pasted(data, public_key_path)})
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
            raise HTTPException(404, "This receipt has no Metal transaction.")
        return JSONResponse(onchain.check(tx, receipt.body.get("metal_credential_ref")).to_json())

    @router.get("/verify/{receipt_id}", response_class=HTMLResponse)
    def receipt_page(receipt_id: str) -> str:
        try:
            receipt, checks = check_receipt(receipt_id, receipts_path, public_key_path, audit_path)
        except KeyError:
            raise HTTPException(404, "No receipt with that id.") from None
        return _page(f"Receipt {receipt.id}", _receipt_detail(receipt, checks))

    def _find(receipt_id: str) -> rc.Receipt:
        for r in rc.load(receipts_path):
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
            f'{html.escape(action)}</span><span class="mono">{html.escape(r.id)}</span>'
            f'<span>{html.escape(str(b.get("amount") or ""))} {html.escape(str(b.get("currency") or ""))}</span>'
            f'<span class="muted">{html.escape(str((b.get("payee") or {}).get("name") or ""))}</span>'
            f'<span class="muted">{html.escape(str(b.get("settlement_status") or ""))}</span></a>')


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
          <button class="btn btn-sm" onclick="onchainCheck()">Verify on Metal testnet</button></div>
          <script>
          async function onchainCheck() {{
            const box = document.getElementById('onchain');
            box.innerHTML = '<span>…</span><b>On chain</b> Reading the Metal testnet…';
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
        ("Metal transaction", e.get("transaction")), ("Airi mandate", e.get("airi_mandate_id")),
        ("Mandate hash", b.get("mandate_hash")), ("Evidence hash", b.get("evidence_hash")),
        ("Receipt hash", receipt.receipt_hash), ("Previous receipt", b.get("prev_receipt_hash")),
        ("Audit entry", (b.get("audit") or {}).get("entry_hash"))] if v]
    id_html = "".join(f'<div class="kv"><span>{html.escape(k)}</span><span class="mono">'
                      + (f'<a href="{html.escape(e["explorer"])}" target="_blank" rel="noopener">{html.escape(str(v))}</a>'
                         if k == "Metal transaction" and e.get("explorer") else html.escape(str(v)))
                      + "</span></div>" for k, v in ids)
    field_html = "".join(f'<div class="kv"><span>{k}</span><span>{v}</span></div>' for k, v in fields)
    return f"""
      <section class="panel verdict {'ok' if all_ok else 'bad'}">
        <h2>{'✓ Authentic receipt' if all_ok else '✗ This receipt does not verify'}</h2>
        <p class="muted mono">{html.escape(receipt.id)} · issued {html.escape(str(b.get("issued_at")))}
          · signer {html.escape(str(b.get("signer")))}</p>
        <div class="checks">{check_html}{onchain}</div>
      </section>
      <section class="panel"><h2>What was authorised, and what happened</h2>{field_html}</section>
      <section class="panel"><h2>Bound identifiers</h2>{id_html}
        <p><a href="/verify/{html.escape(receipt.id)}/json">Download receipt JSON</a> · <a href="/verify">All receipts</a></p>
      </section>"""


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)} · Warden</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400&display=swap" rel="stylesheet">
<style>{_CSS}</style></head><body><main>
<header><a href="/verify" class="brand">Warden <span>Receipt verification</span></a>
<p class="muted">Check any Intent Receipt without trusting Warden: the signature, the chain and, for Metal payments, the chain itself.</p></header>
{body}</main></body></html>"""


_CSS = """
:root { --bg:#0b0f14; --surface:#121821; --surface-2:#18202b; --border:#232d3a; --text:#e6edf3; --muted:#8b98a8;
  --green:#34d399; --green-bg:rgba(52,211,153,.12); --amber:#fbbf24; --red:#f87171; --red-bg:rgba(248,113,113,.12);
  --crimson:#e11d48; --crimson-bg:rgba(225,29,72,.16); --blue:#60a5fa; --mono:"JetBrains Mono",ui-monospace,Consolas,monospace; }
* { box-sizing:border-box } body { margin:0; background:var(--bg); color:var(--text);
  font:15px/1.5 Inter,system-ui,"Segoe UI",sans-serif; -webkit-font-smoothing:antialiased }
main { max-width:920px; margin:0 auto; padding:32px 20px 64px }
header { margin-bottom:20px } .brand { color:var(--text); text-decoration:none; font-size:22px; font-weight:600 }
.brand span { color:var(--muted); font-weight:400; margin-left:8px; font-size:16px }
h2 { font-size:16px; font-weight:600; margin:0 0 12px } a { color:var(--blue) }
.panel { background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:18px 20px; margin-bottom:16px }
.verdict.ok { border-color:rgba(52,211,153,.45) } .verdict.ok h2 { color:var(--green) }
.verdict.bad { border-color:rgba(248,113,113,.5) } .verdict.bad h2 { color:var(--red) }
.muted { color:var(--muted) } .mono { font-family:var(--mono); font-size:13px; word-break:break-all }
.check { display:flex; gap:10px; align-items:baseline; flex-wrap:wrap; padding:8px 10px; border-radius:8px;
  background:var(--surface-2); margin-bottom:6px } .check b { min-width:110px }
.check.ok span:first-child { color:var(--green) } .check.bad span:first-child { color:var(--red) }
.check ul { flex-basis:100%; margin:6px 0 0; color:var(--muted); font-size:13px }
.kv { display:grid; grid-template-columns:160px 1fr; gap:12px; padding:7px 0; border-bottom:1px solid var(--border) }
.kv span:first-child { color:var(--muted) }
.badge { font-size:12px; font-weight:600; padding:2px 8px; border-radius:6px; background:var(--surface-2) }
.badge.allow { color:var(--green); background:var(--green-bg) } .badge.block { color:var(--red); background:var(--red-bg) }
.badge.contain { color:#fda4af; background:var(--crimson-bg) }
.rows .row { display:grid; grid-template-columns:90px 190px 110px 1fr 140px; gap:10px; padding:8px 6px;
  border-bottom:1px solid var(--border); color:var(--text); text-decoration:none; align-items:center }
.rows .row:hover { background:var(--surface-2) }
.btn { background:var(--surface-2); color:var(--text); border:1px solid var(--border); border-radius:8px;
  padding:8px 14px; font:inherit; cursor:pointer } .btn-sm { padding:4px 10px; font-size:13px }
.lookup { display:flex; gap:8px; margin-bottom:10px } input, textarea { flex:1; width:100%; background:var(--bg);
  color:var(--text); border:1px solid var(--border); border-radius:8px; padding:8px 10px; font:13px var(--mono) }
textarea { margin:8px 0 } pre { background:var(--bg); padding:12px; border-radius:8px; overflow:auto; font:12px var(--mono) }
@media (max-width:640px) { .rows .row { grid-template-columns:1fr 1fr } .kv { grid-template-columns:1fr } }
"""
