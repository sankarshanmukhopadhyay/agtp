"""
HTML renderer for Agent Documents (v2 schema).

Produces the visual "identity card" for an agent. The output is a
single self-contained HTML document with embedded CSS; no external
dependencies, no JavaScript, renders correctly in any browser.

The visual layout is deliberately simple and consistent: this becomes
the recognizable look of AGTP agents the way the SSL padlock became
the recognizable look of HTTPS sites.

The v2 card surfaces three primary sections:

  * Skills (prose) - what the agent does, in human language.
  * Methods Needed (requires.methods) - the dispatch surface.
  * Scopes (requires.scopes) - authority tokens the agent expects.

A wildcards badge calls out orchestrators that accept any method.
The footer carries issuer, issued_at, agent ID, and the AGTP version.
"""

from __future__ import annotations

import html

from core.identity import AgentDocument


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
    max-width: 640px;
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
    margin: 24px 0 8px;
  }}
  .badge-row {{
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin: 4px 0 8px;
  }}
  .badge {{
    display: inline-block;
    padding: 3px 10px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.03em;
    text-transform: uppercase;
  }}
  .badge.wildcards-on   {{ background: #fff3cd; color: #856404; }}
  .badge.wildcards-off  {{ background: #e7f3ff; color: #1f5fa6; }}
  .badge.migrated       {{ background: #f5e6ff; color: #5b3a8b; }}
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
  .skill-list {{
    margin: 0;
    padding: 0;
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }}
  .skill-list li {{
    display: flex;
    gap: 10px;
    align-items: flex-start;
    padding: 10px 12px;
    background: #fbfbfd;
    border: 1px solid #ececef;
    border-radius: 8px;
    font-size: 14px;
    line-height: 1.45;
    color: #1d1d1f;
  }}
  .skill-icon {{
    flex-shrink: 0;
    width: 16px;
    height: 16px;
    margin-top: 2px;
    color: #6aa6ff;
  }}
  .method-list, .scope-list {{
    margin: 0;
    padding: 0;
    list-style: none;
  }}
  .method-list li, .scope-list li {{
    padding: 8px 0;
    border-bottom: 1px solid #f0f0f2;
    font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
    font-size: 14px;
    color: #1d1d1f;
  }}
  .method-list li:last-child,
  .scope-list li:last-child {{
    border-bottom: none;
  }}
  .subsection-label {{
    font-size: 11px;
    font-weight: 600;
    color: #6e6e73;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin: 16px 0 8px;
  }}
  .subsection-label:first-child {{
    margin-top: 0;
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
    <div class="badge-row">{badges_html}</div>

    <div class="section">
      <div class="section-label">Skills</div>
      <ul class="skill-list">
        {skills_html}
      </ul>
    </div>

    <div class="section">
      <div class="section-label">Requires</div>

      <div class="subsection-label">Methods Needed</div>
      <ul class="method-list">
        {methods_html}
      </ul>

      <div class="subsection-label">Scopes</div>
      <ul class="scope-list">
        {requires_scopes_html}
      </ul>
    </div>

    <div class="section">
      <div class="section-label">Accepts Inbound Scopes</div>
      <ul class="scope-list">
        {scopes_accepted_html}
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
        <span class="footer-label">Document version</span>
        <span>{document_version}</span>
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


_SKILL_ICON_SVG = (
    '<svg class="skill-icon" viewBox="0 0 16 16" fill="currentColor" '
    'xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'
    '<path d="M8 1l1.9 4.5L14.5 6l-3.4 3.1L12 14 8 11.5 4 14l.9-4.9L1.5 6l4.6-.5L8 1z"/>'
    "</svg>"
)


def _render_list(
    items, *, css_class: str, empty_label: str = "none declared"
) -> str:
    if not items:
        return f'<li style="color:#86868b">{empty_label}</li>'
    return "\n        ".join(
        f'<li class="{css_class}-item">{html.escape(str(i))}</li>' for i in items
    )


def _render_skills(skills) -> str:
    if not skills:
        return '<li style="color:#86868b">none declared</li>'
    rendered = []
    for s in skills:
        rendered.append(
            f'<li>{_SKILL_ICON_SVG}<span>{html.escape(str(s))}</span></li>'
        )
    return "\n        ".join(rendered)


def _render_badges(doc: AgentDocument) -> str:
    badges = []
    if doc.requires.wildcards:
        badges.append(
            '<span class="badge wildcards-on">Wildcard '
            '(accepts any method)</span>'
        )
    else:
        badges.append(
            '<span class="badge wildcards-off">Strict '
            '(declared methods only)</span>'
        )
    if doc.is_migrated:
        badges.append(
            '<span class="badge migrated">Migrated from v1</span>'
        )
    return "".join(badges)


def render_html(doc: AgentDocument) -> str:
    """Render an AgentDocument as a self-contained HTML page."""
    fg, bg, label = STATUS_STYLES.get(
        doc.status.lower(), ("#1d1d1f", "#e8e8e8", doc.status.title())
    )

    return _TEMPLATE.format(
        title=html.escape(f"{doc.name} - AGTP Agent"),
        name=html.escape(doc.name),
        principal=html.escape(doc.principal),
        description=html.escape(doc.description),
        status_label=html.escape(label),
        status_fg=fg,
        status_bg=bg,
        badges_html=_render_badges(doc),
        skills_html=_render_skills(doc.skills),
        methods_html=_render_list(doc.requires.methods, css_class="method"),
        requires_scopes_html=_render_list(doc.requires.scopes, css_class="scope"),
        scopes_accepted_html=_render_list(doc.scopes_accepted, css_class="scope"),
        issuer=html.escape(doc.issuer),
        issued_at=html.escape(doc.issued_at),
        document_version=html.escape(doc.document_version),
        agtp_version=html.escape(doc.agtp_version),
        agent_id=html.escape(doc.agent_id),
    )
