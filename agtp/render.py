"""
HTML renderer for Agent Documents.

Produces the visual "identity card" for an agent. The output is a
single self-contained HTML document with embedded CSS; no external
dependencies, no JavaScript, renders correctly in any browser.

The visual layout is deliberately simple and consistent: this becomes
the recognizable look of AGTP agents the way the SSL padlock became
the recognizable look of HTTPS sites.
"""

from __future__ import annotations

import html

from agtp.identity import AgentDocument


# Status colors. Matched against status strings in the Agent Document.
STATUS_STYLES = {
    "active": ("#0ea65d", "#d4f4e2", "Active"),
    "suspended": ("#c46a00", "#ffeacd", "Suspended"),
    "retired": ("#666666", "#e8e8e8", "Retired"),
}


_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 Helvetica, Arial, sans-serif;
    background: #f5f5f7;
    color: #1d1d1f;
    margin: 0;
    padding: 40px 20px;
    display: flex;
    justify-content: center;
    min-height: 100vh;
    box-sizing: border-box;
  }}
  .card {{
    background: #ffffff;
    border-radius: 14px;
    box-shadow: 0 2px 12px rgba(0, 0, 0, 0.06),
                0 0 0 1px rgba(0, 0, 0, 0.04);
    max-width: 580px;
    width: 100%;
    padding: 32px 36px;
    box-sizing: border-box;
  }}
  .header {{
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
    margin-bottom: 8px;
  }}
  .name {{
    font-size: 28px;
    font-weight: 600;
    letter-spacing: -0.02em;
    margin: 0;
  }}
  .principal {{
    font-size: 15px;
    color: #6e6e73;
    margin: 4px 0 0;
  }}
  .status {{
    display: inline-block;
    padding: 4px 12px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    background: {status_bg};
    color: {status_fg};
    flex-shrink: 0;
  }}
  .description {{
    font-size: 16px;
    line-height: 1.5;
    color: #424245;
    margin: 24px 0;
  }}
  .section {{
    margin-top: 28px;
  }}
  .section-label {{
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: #86868b;
    margin-bottom: 12px;
  }}
  .capability-list, .scope-list {{
    margin: 0;
    padding: 0;
    list-style: none;
  }}
  .capability-list li, .scope-list li {{
    padding: 8px 0;
    border-bottom: 1px solid #f0f0f2;
    font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
    font-size: 14px;
    color: #1d1d1f;
  }}
  .capability-list li:last-child,
  .scope-list li:last-child {{
    border-bottom: none;
  }}
  .footer {{
    margin-top: 32px;
    padding-top: 20px;
    border-top: 1px solid #f0f0f2;
    font-size: 12px;
    color: #86868b;
    line-height: 1.6;
  }}
  .footer-row {{
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 12px;
  }}
  .footer-label {{
    font-weight: 500;
    color: #6e6e73;
  }}
  .agent-id {{
    font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
    font-size: 11px;
    word-break: break-all;
    color: #86868b;
    margin-top: 12px;
    padding: 10px 12px;
    background: #f5f5f7;
    border-radius: 6px;
  }}
  .meta {{
    display: flex;
    align-items: center;
    gap: 6px;
    color: #6e6e73;
    font-size: 13px;
    margin-top: 4px;
  }}
  .meta-icon {{
    width: 14px;
    height: 14px;
    flex-shrink: 0;
  }}
</style>
</head>
<body>
  <main class="card">
    <div class="header">
      <div>
        <h1 class="name">{name}</h1>
        <p class="principal">agent serving {principal}</p>
      </div>
      <span class="status">{status_label}</span>
    </div>

    <p class="description">{description}</p>

    <div class="section">
      <div class="section-label">Capabilities</div>
      <ul class="capability-list">
        {capabilities_html}
      </ul>
    </div>

    <div class="section">
      <div class="section-label">Accepted Scopes</div>
      <ul class="scope-list">
        {scopes_html}
      </ul>
    </div>

    <div class="footer">
      <div class="footer-row">
        <span class="footer-label">Issued by</span>
        <span>{issuer}</span>
      </div>
      <div class="footer-row">
        <span class="footer-label">Issued at</span>
        <span>{issued_at}</span>
      </div>
      <div class="footer-row">
        <span class="footer-label">AGTP version</span>
        <span>{agtp_version}</span>
      </div>
      <div class="agent-id">{agent_id}</div>
    </div>
  </main>
</body>
</html>
"""


def render_html(doc: AgentDocument) -> str:
    """
    Render an AgentDocument as a self-contained HTML page.
    """
    fg, bg, label = STATUS_STYLES.get(
        doc.status.lower(), ("#1d1d1f", "#e8e8e8", doc.status.title())
    )

    capabilities_html = (
        "\n        ".join(f"<li>{html.escape(c)}</li>" for c in doc.capabilities)
        if doc.capabilities
        else '<li style="color:#86868b">none declared</li>'
    )
    scopes_html = (
        "\n        ".join(f"<li>{html.escape(s)}</li>" for s in doc.scopes_accepted)
        if doc.scopes_accepted
        else '<li style="color:#86868b">none declared</li>'
    )

    return _TEMPLATE.format(
        title=html.escape(f"{doc.name} — AGTP Agent"),
        name=html.escape(doc.name),
        principal=html.escape(doc.principal),
        description=html.escape(doc.description),
        status_label=html.escape(label),
        status_fg=fg,
        status_bg=bg,
        capabilities_html=capabilities_html,
        scopes_html=scopes_html,
        issuer=html.escape(doc.issuer),
        issued_at=html.escape(doc.issued_at),
        agtp_version=html.escape(doc.agtp_version),
        agent_id=html.escape(doc.agent_id),
    )
