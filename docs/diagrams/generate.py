#!/usr/bin/env python3
"""Generate the architecture SVG diagrams for docs/architecture.md.

Hand-placed layouts (no auto-layout) so the diagrams stay readable.
Run from anywhere:  python3 docs/diagrams/generate.py
Outputs *.svg next to this file.
"""

import os

OUT = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- palette
SLATE = "#334155"
SLATE_LT = "#64748B"
INK = "#0F172A"
BORDER = "#94A3B8"
CHIP_BORDER = "#CBD5E1"

BLUE, BLUE_T = "#2563EB", "#EFF6FF"      # managed fleet
AMBER, AMBER_T = "#B45309", "#FFFBEB"    # corporate network
GREEN, GREEN_T = "#047857", "#ECFDF5"    # AWS VPC
VIOLET, VIOLET_T = "#6D28D9", "#F5F3FF"  # AWS regional services
RED, RED_T = "#B91C1C", "#FEF2F2"        # external / sensitive

FONT = "Helvetica, Arial, sans-serif"


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SVG:
    def __init__(self, w, h, title, subtitle=""):
        self.w, self.h = w, h
        self.body = []
        self.defs_done = False
        self.add(f'<rect x="0.5" y="0.5" width="{w-1}" height="{h-1}" rx="14" '
                 f'fill="#FFFFFF" stroke="#E2E8F0"/>')
        self.text(36, 46, title, size=20, weight="bold", color=INK)
        if subtitle:
            self.text(36, 68, subtitle, size=12.5, color=SLATE_LT)

    def add(self, s):
        self.body.append(s)

    def text(self, x, y, s, size: float = 11, color=SLATE, weight="normal",
             anchor="start", style=""):
        self.add(f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" '
                 f'fill="{color}" font-weight="{weight}" text-anchor="{anchor}" '
                 f'{style}>{esc(s)}</text>')

    def zone(self, x, y, w, h, label, color, tint, label2=""):
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" '
                 f'fill="{tint}" stroke="{color}" stroke-width="1.5"/>')
        self.text(x + 16, y + 26, label.upper(), size=12, color=color,
                  weight="bold", style='letter-spacing="0.06em"')
        if label2:
            self.text(x + 16, y + 43, label2, size=10.5, color=color)

    def node(self, x, y, w, h, title, lines=(), border=BORDER, fill="#FFFFFF",
             tsize: float = 13, cyl=False, dashed=False):
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        if cyl:  # simple database cylinder
            ry = 9
            self.add(f'<path d="M{x} {y+ry} a {w/2} {ry} 0 0 1 {w} 0 '
                     f'v {h-2*ry} a {w/2} {ry} 0 0 1 -{w} 0 Z" '
                     f'fill="{fill}" stroke="{border}" stroke-width="1.4"{dash}/>')
            self.add(f'<path d="M{x} {y+ry} a {w/2} {ry} 0 0 0 {w} 0" '
                     f'fill="none" stroke="{border}" stroke-width="1.4"/>')
            ty = y + ry + 22
        else:
            self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" '
                     f'fill="{fill}" stroke="{border}" stroke-width="1.4"{dash}/>')
            ty = y + 21
        cx = x + w / 2
        self.text(cx, ty, title, size=tsize, color=INK, weight="bold",
                  anchor="middle")
        for i, ln in enumerate(lines):
            self.text(cx, ty + 16 + i * 14, ln, size=10.5, color=SLATE_LT,
                      anchor="middle")

    def arrow(self, pts, color=SLATE, dashed=False, width=1.6):
        d = "M" + " L".join(f"{px} {py}" for px, py in pts)
        dash = ' stroke-dasharray="6 5"' if dashed else ""
        self.add(f'<path d="{d}" fill="none" stroke="{color}" '
                 f'stroke-width="{width}"{dash} marker-end="url(#arr)"/>')

    def chip(self, cx, cy, s, color=SLATE, border=CHIP_BORDER, size=10.5,
             weight="normal"):
        w = 6.3 * len(s) * (size / 10.5) + 16
        self.add(f'<rect x="{cx - w/2}" y="{cy - 10}" width="{w}" height="19" '
                 f'rx="5" fill="#FFFFFF" stroke="{border}"/>')
        self.text(cx, cy + 3.5, s, size=size, color=color, anchor="middle",
                  weight=weight)

    def badge(self, cx, cy, s, color="#FFFFFF", fill=SLATE):
        self.add(f'<circle cx="{cx}" cy="{cy}" r="10" fill="{fill}"/>')
        self.text(cx, cy + 3.8, s, size=11, color=color, weight="bold",
                  anchor="middle")

    def write(self, name):
        head = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" '
            f'height="{self.h}" viewBox="0 0 {self.w} {self.h}">\n'
            f'<defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" '
            f'markerWidth="7.5" markerHeight="7.5" orient="auto-start-reverse">'
            f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{SLATE}"/></marker></defs>\n'
        )
        path = os.path.join(OUT, name)
        with open(path, "w") as f:
            f.write(head + "\n".join(self.body) + "\n</svg>\n")
        print("wrote", name)


# =====================================================================
# 1. System architecture & trust boundaries
# =====================================================================
def d1():
    s = SVG(1500, 950, "System architecture & trust boundaries",
            "Claude apps gateway in AWS GovCloud us-gov-west-1 — every arrow is TLS unless flagged")

    s.zone(40, 96, 340, 216, "Managed Windows fleet", BLUE, BLUE_T, "Zscaler ZPA")
    s.node(64, 138, 292, 74, "Developer laptop — Claude Code",
           ["precompiled binary, mirrored install",
            "managed settings: forced gateway login,", "updates disabled"],
           border=BLUE)
    s.node(64, 232, 292, 58, "Zscaler Client Connector",
           ["answers DNS with synthetic CGNAT IPs"], border=BLUE)

    s.zone(40, 360, 340, 300, "Corporate network", AMBER, AMBER_T)
    s.node(64, 402, 292, 58, "ZPA App Connector",
           ["real DNS lookup + the source IP", "the ALB actually sees"],
           border=AMBER)
    s.node(64, 480, 292, 58, "AD DNS",
           ["claude-gateway.example.com", "CNAME → internal ALB name"],
           border=AMBER)
    s.node(64, 558, 292, 58, "Egress proxy (optional)",
           ["HTTPS_PROXY_URL — only when the", "landing zone mandates it"],
           border=AMBER, dashed=True)

    s.zone(432, 96, 640, 780, "AWS GovCloud VPC", GREEN, GREEN_T,
           "spoke · private subnets only · no IGW/NAT required")
    s.node(470, 150, 250, 76, "Internal ALB  :443",
           ["enterprise-CA cert (ACM import)", "IPv4-only · deletion-protected",
            "stack-policy locked"], border=GREEN)
    s.node(790, 150, 250, 76, "Grafana  :3000 TLS",
           ["per-task self-signed cert", "Okta SSO only, no local login"],
           border=GREEN)
    # Gateway task ×2 — the ADOT collector runs co-resident as a loopback sidecar
    s.node(470, 284, 570, 104, "Gateway task — ECS Fargate ×2", [], border=GREEN)
    s.node(490, 314, 250, 54, "claude gateway",
           ["pinned binary · TLS listener :8080"], border=GREEN, tsize=12)
    s.node(770, 314, 250, 54, "ADOT collector",
           ["loopback OTLP sidecar"], border=GREEN, dashed=True, tsize=12)
    s.arrow([(740, 341), (770, 341)])
    s.chip(755, 380, "loopback :4318", GREEN, size=9.5)
    s.node(470, 434, 250, 84, "RDS PostgreSQL 16",
           ["Multi-AZ · CMK · pgaudit", "app-user login only",
            "stack-policy locked"], border=GREEN, cyl=True)
    s.node(790, 420, 250, 76, "db-admin Lambdas",
           ["bootstrap app DB users +", "rotate secret & roll service"],
           border=GREEN)
    s.node(470, 540, 250, 76, "Download portal ×2",
           ["Okta OIDC + group authz (PKCE)", "TLS listener :8080, per-task cert"],
           border=GREEN)
    s.node(470, 646, 570, 90, "Interface VPC endpoints — each with a resource policy",
           ["bedrock-runtime (2 approved models only) · ecr.api · ecr.dkr · logs",
            "secretsmanager · ecs · aps-workspaces  +  S3 gateway endpoint"],
           border=GREEN)
    s.node(470, 760, 570, 76, "KMS CMK  alias/<prefix>",
           ["one customer-managed key: RDS, secrets, log groups, activity",
            "archive, AMP, ECR, portal artifacts + audit (rotation enabled)"],
           border=GREEN)

    s.zone(1124, 96, 336, 624, "AWS regional services", VIOLET, VIOLET_T,
           "reached via endpoints / AWS backbone")
    s.node(1148, 152, 288, 56, "Managed Prometheus (AMP)",
           ["usage/cost metrics · CMK"], border=VIOLET)
    s.node(1148, 228, 288, 66, "Amazon Bedrock",
           ["Claude Opus 4.8 · Sonnet 4.5", "us-gov profiles · endpoint policy: 2 models"],
           border=VIOLET)
    s.node(1148, 314, 288, 48, "Secrets Manager (CMK)", [], border=VIOLET)
    s.node(1148, 382, 288, 56, "CloudWatch Logs → Firehose",
           ["activity window (CMK) → archive"], border=VIOLET)
    s.node(1148, 458, 288, 56, "S3: activity archive",
           ["SSE-KMS · 731-day retention"], border=VIOLET, cyl=True)
    s.node(1148, 534, 288, 56, "S3: ALB access logs",
           ["SSE-S3 — ELB delivery limitation"], border=VIOLET, cyl=True)
    s.node(1148, 600, 288, 50, "S3: portal artifacts (04)",
           ["SSE-KMS · installers + manifest"], border=VIOLET, cyl=True)
    s.node(1148, 662, 288, 50, "CloudWatch: portal-audit (04)",
           ["CMK · 365 d · flag for SIEM"], border=VIOLET)

    s.zone(1124, 742, 336, 120, "External SaaS — the only public dependency",
           RED, RED_T)
    s.node(1148, 786, 288, 58, "Okta",
           ["authorization server (org or custom)", "returns groups in token"], border=RED)

    # ---- flows
    s.arrow([(210, 212), (210, 232)])
    s.arrow([(210, 290), (210, 360)])
    s.chip(210, 336, "ZPA tunnel", BLUE)
    s.arrow([(356, 431), (415, 431), (415, 188), (470, 188)])
    s.chip(415, 240, "TLS :443", GREEN, weight="bold")
    s.arrow([(720, 188), (790, 188)])
    s.chip(755, 176, ":3000", GREEN)
    s.arrow([(595, 226), (595, 284)])
    s.chip(595, 262, ":8080 re-encrypt", GREEN)
    s.arrow([(595, 388), (595, 434)])
    s.chip(595, 404, ":5432 verify-full", GREEN)
    # Grafana -> AMP (straight into the top row)
    s.arrow([(1040, 178), (1148, 178)])
    s.chip(1094, 166, "SigV4 query", VIOLET)
    # gateway -> Bedrock (inference); leaves the wide task box's right edge
    s.arrow([(1040, 330), (1112, 330), (1112, 262), (1148, 262)])
    s.chip(1093, 288, "inference — SigV4", VIOLET)
    # gateway task (telemetry sidecar) -> AMP (remote_write via the aps endpoint)
    s.arrow([(1040, 305), (1088, 305), (1088, 200), (1148, 200)])
    s.chip(1086, 224, "SigV4 remote_write", VIOLET)
    # gateway task (telemetry sidecar) -> CloudWatch (activity stream)
    s.arrow([(1040, 360), (1128, 360), (1128, 410), (1148, 410)])
    s.chip(1088, 388, "activity (opt-in)", VIOLET)
    # db-admin -> RDS and -> Secrets Manager
    s.arrow([(790, 470), (720, 470)])
    s.arrow([(915, 496), (915, 520), (1136, 520), (1136, 338), (1148, 338)])
    s.chip(1024, 520, "manage app secret", VIOLET)
    # ALB -> portal (:8080 re-encrypt), down the inter-column gap into portal's edge
    s.arrow([(720, 190), (755, 190), (755, 578), (720, 578)])
    s.chip(758, 512, ":8080 · /portal", GREEN)
    # portal -> S3 artifacts and -> download-audit (right column free band + gap)
    s.arrow([(720, 555), (1092, 555), (1092, 622), (1148, 622)])
    s.chip(905, 543, "installers · S3 gw endpoint", VIOLET)
    s.arrow([(720, 600), (1080, 600), (1080, 684), (1148, 684)])
    s.chip(900, 588, "download-audit (CMK)", VIOLET)
    # gateway + portal -> Okta (down the VPC's clear left margin, out the bottom)
    s.arrow([(470, 330), (452, 330), (452, 858), (1200, 858), (1200, 844)])
    s.arrow([(470, 578), (452, 578)])           # portal joins the same OIDC riser
    s.chip(800, 858, "gateway + portal — OIDC login + token exchange (TGW egress or proxy)", RED)
    # Grafana -> Okta
    s.arrow([(940, 226), (940, 274), (1096, 274), (1096, 800), (1148, 800)])
    s.chip(1090, 732, "OAuth code exchange", RED)
    # optional proxy path
    s.arrow([(356, 587), (410, 587), (410, 878), (1240, 878), (1240, 844)],
            dashed=True)
    s.chip(700, 878, "proxy path when mandated", AMBER)

    s.text(36, 916, "Boundary facts: no public ingress (internal ALB behind ZPA); "
           "no public egress from the inference path (Bedrock endpoint, 2-model policy); "
           "clients never contact Anthropic (mirrored, verified binaries; updates disabled).",
           size=11.5, color=SLATE)
    s.write("01-system-architecture.svg")


# =====================================================================
# 2. Network flows, ports & TLS state
# =====================================================================
def d2():
    hops = [
        # n, source, src color, protocol label, dest, dest color, red?
        (1, "Claude Code (laptop, via ZPA)", BLUE,
         "TLS :443 — enterprise cert, fingerprint-pinned · ZPA carries, never inspects",
         "Internal ALB", GREEN, False),
        (2, "Internal ALB", GREEN,
         "TLS :8080 — ALB re-encrypt, per-task self-signed cert",
         "Gateway task", GREEN, False),
        (3, "Internal ALB", GREEN,
         "TLS :3000 — ALB re-encrypt, per-task self-signed cert",
         "Grafana task", GREEN, False),
        (4, "Gateway task", GREEN,
         "TLS :5432 — sslmode=verify-full, RDS CA bundle baked into the image",
         "RDS PostgreSQL", GREEN, False),
        (5, "Gateway task", GREEN,
         "TLS :443 + SigV4 — via bedrock-runtime endpoint, 2 approved models only",
         "Amazon Bedrock", VIOLET, False),
        (6, "Gateway task", GREEN,
         "LOOPBACK 127.0.0.1:4318 OTLP — co-resident sidecar, never on the network",
         "ADOT collector (same task)", GREEN, False),
        (7, "Gateway task (sidecar)", GREEN,
         "TLS :443 + SigV4 — Prometheus remote_write via aps-workspaces endpoint",
         "Managed Prometheus", VIOLET, False),
        (8, "Grafana task", GREEN,
         "TLS :443 + SigV4 — PromQL queries",
         "Managed Prometheus", VIOLET, False),
        (9, "Gateway task", GREEN,
         "TLS :443 — OIDC token exchange (TGW central egress or corporate proxy)",
         "Okta (public SaaS)", RED, False),
        (10, "Grafana task", GREEN,
         "TLS :443 — OAuth code exchange (same egress path)",
         "Okta (public SaaS)", RED, False),
        # ---- download portal (stack 04) — appended so hops 1–10 keep their numbers
        (11, "Browser (laptop, via ZPA)", BLUE,
         "TLS :443 — enterprise cert · GET https://<fqdn>/portal (path rule, pri 20)",
         "Internal ALB", GREEN, False),
        (12, "Internal ALB", GREEN,
         "TLS :8080 — ALB re-encrypt, per-task self-signed cert",
         "Portal task", GREEN, False),
        (13, "Portal task", GREEN,
         "TLS :443 — OIDC auth-code + PKCE + JWKS (TGW central egress or proxy)",
         "Okta (public SaaS)", RED, False),
        (14, "Portal task", GREEN,
         "TLS :443 + SigV4 — installer read via S3 gateway endpoint",
         "S3 artifacts (CMK)", VIOLET, False),
        (15, "Portal task", GREEN,
         "TLS :443 + SigV4 — download-audit stream via logs endpoint",
         "CloudWatch (portal-audit)", VIOLET, False),
    ]
    top, step, rh = 120, 50, 38
    gap = 42          # extra space opening the download-portal section
    n_before = 10     # rows 1–10 precede the portal group
    s = SVG(1400, top + step * len(hops) + gap + 80,
            "Network flows, ports & TLS state — every hop in the system",
            "one row per flow; row 6 is an in-task loopback hop (never on the "
            "network) · rows 11–15 are the download portal (stack 04)")
    for i, (n, src, sc, proto, dst, dc, red) in enumerate(hops):
        extra = gap if i >= n_before else 0
        y = top + i * step + extra
        if i == n_before:
            dy = y - 26
            s.add(f'<line x1="36" y1="{dy}" x2="1364" y2="{dy}" stroke="{CHIP_BORDER}" '
                  f'stroke-width="1.2" stroke-dasharray="6 5"/>')
            s.text(64, dy - 8, "DOWNLOAD PORTAL — stack 04 · path rule /portal on "
                   "the shared ALB · deployed after 02, independent of 03",
                   size=11, color=SLATE_LT, weight="bold",
                   style='letter-spacing="0.04em"')
        if red:
            s.add(f'<rect x="36" y="{y - 6}" width="1328" height="{rh + 12}" '
                  f'rx="8" fill="{RED_T}"/>')
        s.badge(64, y + rh / 2, str(n))
        s.add(f'<rect x="92" y="{y}" width="270" height="{rh}" rx="7" '
              f'fill="#FFFFFF" stroke="{sc}" stroke-width="1.3"/>')
        s.text(227, y + rh / 2 + 4, src, size=11.5, color=INK, weight="bold",
               anchor="middle")
        s.arrow([(370, y + rh / 2), (1090, y + rh / 2)],
                color=RED if red else SLATE)
        s.chip(730, y + rh / 2, proto, RED if red else SLATE)
        s.add(f'<rect x="1098" y="{y}" width="266" height="{rh}" rx="7" '
              f'fill="#FFFFFF" stroke="{dc}" stroke-width="1.3"/>')
        s.text(1231, y + rh / 2 + 4, dst, size=11.5, color=INK, weight="bold",
               anchor="middle")
    fy = top + step * len(hops) + gap + 36
    s.text(48, fy, "TLS termination points: developer→ALB terminates on the "
           "enterprise cert (developers pin its fingerprint); ALB→task hops "
           "terminate on per-task ephemeral certs (ALBs do not validate "
           "target certs; keys never leave the task).", size=11.5, color=SLATE)
    s.text(48, fy + 20, "All AWS-service hops (5, 7, 8, 14, 15) also carry SigV4 "
           "request signing on top of TLS. DNS to the VPC resolver is exempt "
           "from security-group evaluation (AWS platform behavior).",
           size=11.5, color=SLATE_LT)
    s.write("02-network-flows-tls.svg")


# =====================================================================
# 3. Developer authentication (sequence)
# =====================================================================
def seq_canvas(title, sub, lanes, h):
    s = SVG(1240, h, title, sub)
    xs = {}
    for name, x, color, lines in lanes:
        xs[name] = x
        s.node(x - 95, 92, 190, 40 + 14 * len(lines), name, lines,
               border=color, tsize=12.5)
        s.add(f'<path d="M{x} {132 + 14*len(lines)} V {h - 70}" stroke="{CHIP_BORDER}" '
              f'stroke-width="1.2" stroke-dasharray="4 5" fill="none"/>')
    return s, xs


def seq_msg(s, xs, n, frm, to, y, label, color=SLATE, dashed=False):
    x1, x2 = xs[frm], xs[to]
    s.arrow([(x1, y), (x2, y)], dashed=dashed)
    mid = (x1 + x2) / 2
    # badge sits left of the leftmost lifeline so wide chips can't cover it
    s.badge(min(x1, x2) - 36, y - 14, str(n))
    s.chip(mid, y - 14, label, color)


def d3():
    lanes = [
        ("Claude Code", 160, BLUE, ["laptop CLI"]),
        ("Browser", 430, BLUE, ["laptop"]),
        ("Gateway", 740, GREEN, ["reached via ALB + ZPA,", "TLS end-to-end"]),
        ("Okta", 1050, RED, ["org / custom auth server"]),
    ]
    s, xs = seq_canvas("Developer authentication — Okta OIDC via the gateway",
                       "managed settings force gateway login; no local API keys exist",
                       lanes, 890)
    y = 210
    seq_msg(s, xs, 1, "Claude Code", "Gateway", y, "/login → device authorization"); y += 48
    seq_msg(s, xs, 2, "Gateway", "Claude Code", y, "verification URL + user code", dashed=True); y += 48
    seq_msg(s, xs, 3, "Claude Code", "Browser", y, "open auth URL", dashed=True); y += 48
    seq_msg(s, xs, 4, "Browser", "Gateway", y, "GET /oauth/authorize"); y += 48
    seq_msg(s, xs, 5, "Gateway", "Browser", y, "302 → Okta /v1/authorize", dashed=True); y += 48
    seq_msg(s, xs, 6, "Browser", "Okta", y, "authenticate — Okta MFA & policy"); y += 48
    seq_msg(s, xs, 7, "Okta", "Browser", y, "302 → /oauth/callback?code=…", dashed=True); y += 48
    seq_msg(s, xs, 8, "Browser", "Gateway", y, "callback with code"); y += 48
    seq_msg(s, xs, 9, "Gateway", "Okta", y, "code → token (client secret; TGW/proxy)"); y += 48
    seq_msg(s, xs, 10, "Okta", "Gateway", y, "id_token: email, groups", dashed=True); y += 40
    s.node(630, y, 220, 66, "Gateway checks",
           ["allowed email domains;", "signs session JWT (1 h TTL)"],
           border=GREEN, fill=GREEN_T, tsize=11.5)
    y += 92
    seq_msg(s, xs, 11, "Gateway", "Claude Code", y, "gateway session established", dashed=True); y += 48
    seq_msg(s, xs, 12, "Claude Code", "Gateway", y, "inference requests with session JWT")
    s.text(36, 858, "Audit identity chain: Okta identity → session JWT → user.id / user.email / "
           "user.groups stamped on every request and telemetry export. Grafana repeats this flow "
           "with its own client + strict group→role mapping.", size=11.5, color=SLATE)
    s.write("03-developer-authentication-oidc.svg")


# =====================================================================
# 4. DB credential lifecycle (sequence, two phases)
# =====================================================================
def d4():
    lanes = [
        ("CloudFormation", 130, SLATE, [""]),
        ("Bootstrap Lambda", 360, GREEN, ["db-admin image"]),
        ("Secrets Manager", 600, VIOLET, ["CMK-encrypted"]),
        ("RDS Postgres", 830, GREEN, [""]),
        ("Rotation Lambda", 1060, GREEN, ["db-admin image"]),
    ]
    s, xs = seq_canvas("Database credential lifecycle — least privilege + alternating-users rotation",
                       "the gateway never holds the master credential; the master secret is break-glass only",
                       lanes, 940)

    s.add(f'<rect x="60" y="176" width="1120" height="240" rx="10" fill="{BLUE_T}" opacity="0.55"/>')
    s.text(76, 196, "PHASE 1 — one-time bootstrap (stack create)", size=11.5,
           color=BLUE, weight="bold")
    y = 226
    seq_msg(s, xs, 1, "CloudFormation", "Bootstrap Lambda", y, "Custom::DbAppUserBootstrap"); y += 46
    seq_msg(s, xs, 2, "Bootstrap Lambda", "Secrets Manager", y, "read master secret (rds!…)"); y += 46
    seq_msg(s, xs, 3, "Bootstrap Lambda", "RDS Postgres", y,
            "as master: create owner + app roles, grants, adopt tables"); y += 46
    seq_msg(s, xs, 4, "Bootstrap Lambda", "Secrets Manager", y,
            "write app secret v1 (gateway_app + password)"); y += 40
    s.text(76, y, "→ ECS service is created only after bootstrap succeeds; tasks inject "
           "PGUSER/PGPASSWORD from the app secret at launch", size=11, color=SLATE)

    s.add(f'<rect x="60" y="446" width="1120" height="380" rx="10" fill="{GREEN_T}" opacity="0.7"/>')
    s.text(76, 466, "PHASE 2 — every rotation (immediately at creation, then every 90 days)",
           size=11.5, color=GREEN, weight="bold")
    y = 500
    seq_msg(s, xs, 5, "Secrets Manager", "Rotation Lambda", y, "createSecret"); y += 44
    seq_msg(s, xs, 6, "Rotation Lambda", "Secrets Manager", y,
            "stage AWSPENDING: other user + new password", dashed=True); y += 44
    seq_msg(s, xs, 7, "Secrets Manager", "Rotation Lambda", y, "setSecret"); y += 44
    seq_msg(s, xs, 8, "Rotation Lambda", "RDS Postgres", y,
            "as master: ALTER ROLE other-user PASSWORD"); y += 44
    seq_msg(s, xs, 9, "Secrets Manager", "Rotation Lambda", y, "testSecret → connect as new user"); y += 44
    seq_msg(s, xs, 10, "Secrets Manager", "Rotation Lambda", y, "finishSecret"); y += 44
    seq_msg(s, xs, 11, "Rotation Lambda", "Secrets Manager", y,
            "move AWSCURRENT (idempotent)", dashed=True); y += 44
    s.node(910, y - 14, 290, 46, "then: ECS UpdateService",
           ["force new deployment — tasks re-fetch"], border=GREEN, tsize=11.5)
    y += 56

    s.text(36, 862, "Why alternating users: the PREVIOUS credential stays valid until the NEXT "
           "rotation, so running tasks never hold a dead password — the roll is never "
           "time-critical.", size=11.5, color=SLATE)
    s.text(36, 882, "Failure handling: Secrets Manager retries any failed step; a persistently "
           "failing rotation trips the <prefix>-db-rotation-errors CloudWatch alarm.",
           size=11.5, color=SLATE)
    s.text(36, 910, "Roles: gw (master, break-glass, RDS-rotated weekly) · gateway_owner "
           "(NOLOGIN, owns schema) · gateway_app / gateway_app_clone (LOGIN, SET role → owner; "
           "no CREATEROLE, no rds_superuser, cannot touch pgaudit).",
           size=11.5, color=SLATE)
    s.write("04-db-credential-lifecycle.svg")


# =====================================================================
# 5. Telemetry & audit data flows
# =====================================================================
def d5():
    s = SVG(1500, 700, "Telemetry & audit data flows",
            "two streams, different sensitivity — the audit stream is opt-in and prompt content is always redacted")

    s.node(48, 150, 240, 76, "Claude Code clients",
           ["OTLP enabled centrally by the", "gateway via /managed/settings"],
           border=BLUE)
    s.node(48, 260, 240, 62, "Gateway's own metrics", [], border=GREEN)
    # Gateway task — the ADOT collector is a co-resident loopback sidecar
    s.node(380, 168, 620, 112, "Gateway task — telemetry sidecar co-resident",
           [], border=GREEN)
    s.node(396, 200, 272, 64, "Gateway",
           ["stamps user.id · user.email ·", "user.groups on every export"],
           border=GREEN, tsize=12)
    s.node(692, 200, 290, 64, "ADOT collector — sidecar",
           ["drops session.id · promotes", "team / cost_center to labels"],
           border=GREEN, dashed=True, tsize=12)
    s.arrow([(668, 232), (692, 232)])
    s.chip(680, 274, "loopback", GREEN, size=9.5)

    s.zone(1080, 96, 380, 250, "Usage & cost metrics", VIOLET, VIOLET_T,
           "operational sensitivity")
    s.node(1104, 152, 332, 66, "Amazon Managed Prometheus",
           ["CMK · 150-day retention"], border=VIOLET)
    s.node(1104, 244, 332, 76, "Grafana dashboard",
           ["Okta SSO, strict group→role;", "cost by team / cost-center / Okta group"],
           border=VIOLET)

    s.zone(740, 380, 720, 250, "Activity audit stream — HIGHLY SENSITIVE, OPT-IN",
           RED, RED_T, "FORWARD_ACTIVITY_LOGS=true · bash commands, tool inputs, file paths per user")
    s.node(764, 446, 200, 76, "CloudWatch Logs",
           ["CMK · 14-day", "operational window"], border=RED)
    s.node(1020, 446, 170, 76, "Firehose", ["buffered delivery"], border=RED)
    s.node(1246, 446, 190, 76, "S3 archive",
           ["SSE-KMS · 731 days", "bucket retained"], border=RED, cyl=True)
    s.text(764, 560, "Access: IAM only — no dashboard surface. Flag for SIEM subscription "
           "where policy requires.", size=11, color=RED)

    s.arrow([(288, 195), (380, 220)])
    s.chip(330, 180, "OTLP via gateway FQDN", BLUE)
    s.arrow([(288, 291), (334, 291), (334, 244), (380, 244)])
    # activity stream is emitted by the sidecar's logs exporter
    s.arrow([(748, 264), (748, 484), (764, 484)])
    s.chip(760, 352, "activity records (only when enabled)", RED)
    # remote_write leaves the gateway task (sidecar) to AMP
    s.arrow([(1000, 210), (1104, 185)])
    s.chip(1052, 172, "SigV4 remote_write", VIOLET)
    s.arrow([(1270, 218), (1270, 244)])
    s.arrow([(964, 484), (1020, 484)])
    s.arrow([(1190, 484), (1246, 484)])

    s.text(48, 664, "Data-class table with retention and access paths: architecture.md §5. "
           "ALB access logs (SSE-S3, 90 d) and pgaudit DB logs (CMK, 365 d) are separate "
           "IAM-only stores.", size=11.5, color=SLATE)
    s.write("05-telemetry-audit-data-flows.svg")


# =====================================================================
# 6. Stack dependencies & deploy order
# =====================================================================
def d6():
    s = SVG(1500, 560, "Stack dependencies & deploy order",
            "arrows carry CloudFormation exports — while imported, an export's value is locked (day-one decisions called out)")

    s.node(48, 150, 200, 90, "Certificate",
           ["import-enterprise-cert.sh", "CSR → enterprise CA → ACM"],
           border=AMBER)
    s.node(318, 130, 230, 130, "01-database",
           ["KMS CMK (or bring-your-own)", "RDS PG16 + pgaudit", "db security groups",
            "stack policy: Database locked"], border=GREEN)
    s.node(318, 330, 230, 130, "Image builds (egress host)",
           ["gateway (mirrored binary)", "db-admin Lambda", "Grafana provisioned",
            "ADOT mirror — digest-pinned"], border=BLUE)
    s.node(640, 130, 240, 130, "02-gateway",
           ["ALB + TLS · ECS · IAM · secrets", "VPC endpoints + policies",
            "DB bootstrap + rotation", "stack policy: ALB locked"], border=GREEN)
    s.node(980, 130, 230, 130, "03-observability",
           ["AMP (CMK) + aps endpoint", "Grafana + Okta SSO",
            "activity archive chain"], border=GREEN)
    s.node(1290, 150, 170, 90, "02 re-run",
           ["picks up AMP endpoint;", "sidecar remote-writes", "+ activity stream"],
           border=GREEN)
    s.node(980, 330, 230, 120, "04-download-portal",
           ["Okta OIDC + group authz", "S3 artifacts (CMK) · audit log",
            "ALB /portal rule · own SG"], border=GREEN)

    s.arrow([(248, 195), (318, 195)])
    s.arrow([(433, 260), (433, 330)])
    s.text(421, 290, "KMS_KEY_ARN persisted →", size=10.5, color=VIOLET,
           anchor="end")
    s.text(421, 305, "ECR repos born CMK-encrypted", size=10.5, color=VIOLET,
           anchor="end")
    s.arrow([(548, 195), (640, 195)])
    s.text(594, 284, "exports: kms-key-arn · db-endpoint", size=10.5,
           color=SLATE, anchor="middle")
    s.text(594, 300, "db-secret-arn · db-client-sg", size=10.5, color=SLATE,
           anchor="middle")
    s.add(f'<path d="M594 272 V 208" stroke="{CHIP_BORDER}" stroke-width="1" '
          f'stroke-dasharray="2 3" fill="none"/>')
    s.arrow([(548, 380), (760, 380), (760, 260)])
    s.text(654, 368, "image URIs via deploy.env", size=10.5, color=BLUE)
    s.arrow([(880, 195), (980, 195)])
    s.text(930, 284, "exports: svc-sg · alb-sg", size=10.5, color=SLATE,
           anchor="middle")
    s.text(930, 300, "https-listener · cluster-arn", size=10.5, color=SLATE,
           anchor="middle")
    s.add(f'<path d="M930 272 V 208" stroke="{CHIP_BORDER}" stroke-width="1" '
          f'stroke-dasharray="2 3" fill="none"/>')
    s.arrow([(1210, 195), (1290, 195)])
    s.text(1250, 284, "AMP endpoint + ARN,", size=10.5, color=SLATE,
           anchor="middle")
    s.text(1250, 299, "activity log group", size=10.5, color=SLATE,
           anchor="middle")
    s.add(f'<path d="M1250 272 V 208" stroke="{CHIP_BORDER}" stroke-width="1" '
          f'stroke-dasharray="2 3" fill="none"/>')
    # 02 -> 04 (deploy after 02, independent of 03; imports 02's SGs/listener/cluster).
    # Start right-of-centre on 02's bottom edge to avoid the image-builds->02 line at x760.
    s.arrow([(830, 260), (830, 395), (980, 395)])
    s.text(762, 414, "02 exports to 04:", size=10.5, color=SLATE)
    s.text(762, 429, "alb-sg · endpoint-sg", size=10.5, color=SLATE)
    s.text(762, 444, "https-listener · cluster-arn", size=10.5, color=SLATE)
    s.text(1095, 472, "deploy after 02 · independent of 03",
           size=10, color=SLATE_LT, anchor="middle")

    s.text(48, 500, "Locks a reviewer should know: the RDS storage CMK is fixed at creation "
           "(plus 01↔02 export locks) — a day-one decision; 03 and 04 must be deleted "
           "before 02 replacement-updates; the ALB and Database carry stack policies "
           "denying Update:Replace / Update:Delete.", size=11.5, color=SLATE)
    s.text(48, 522, "Full teardown/update ordering: README — Teardown & update order.",
           size=11.5, color=SLATE_LT)
    s.write("06-stack-dependencies-deploy-order.svg")


# =====================================================================
# 1a. Access & network path (split view)
# =====================================================================
def d1a():
    s = SVG(1500, 760, "Access & network path",
            "how requests reach the gateway and how the two OIDC flows leave — every arrow is TLS")

    s.zone(40, 96, 340, 216, "Managed Windows fleet", BLUE, BLUE_T, "Zscaler ZPA")
    s.node(64, 138, 292, 74, "Developer laptop — Claude Code",
           ["precompiled binary, mirrored install",
            "managed settings: forced gateway login,", "updates disabled"],
           border=BLUE)
    s.node(64, 232, 292, 58, "Zscaler Client Connector",
           ["answers DNS with synthetic CGNAT IPs"], border=BLUE)

    s.zone(40, 360, 340, 300, "Corporate network", AMBER, AMBER_T)
    s.node(64, 402, 292, 58, "ZPA App Connector",
           ["real DNS lookup + the source IP", "the ALB actually sees"],
           border=AMBER)
    s.node(64, 480, 292, 58, "AD DNS",
           ["claude-gateway.example.com", "CNAME → internal ALB name"],
           border=AMBER)
    s.node(64, 558, 292, 58, "Egress proxy (optional)",
           ["HTTPS_PROXY_URL — only when the", "landing zone mandates it"],
           border=AMBER, dashed=True)

    s.zone(440, 96, 600, 560, "AWS GovCloud VPC", GREEN, GREEN_T,
           "spoke · private subnets only")
    s.node(480, 160, 250, 80, "Internal ALB  :443",
           ["enterprise-CA cert (ACM import)", "IPv4-only · deletion-protected",
            "stack-policy locked"], border=GREEN)
    s.node(760, 160, 250, 80, "Grafana  :3000 TLS",
           ["per-task self-signed cert", "Okta SSO only, no local login"],
           border=GREEN)
    s.node(480, 320, 250, 80, "Gateway — ECS Fargate ×2",
           ["claude gateway (pinned binary)", "TLS listener :8080, per-task cert"],
           border=GREEN)
    s.node(480, 480, 250, 90, "RDS PostgreSQL 16",
           ["Multi-AZ · CMK · pgaudit", "app-user login only"],
           border=GREEN, cyl=True)
    s.text(760, 500, "All AWS-service traffic (Bedrock,", size=10.5, color=GREEN)
    s.text(760, 516, "telemetry, secrets, archives) exits via", size=10.5, color=GREEN)
    s.text(760, 532, "VPC endpoints — see the services view.", size=10.5, color=GREEN)

    s.zone(1120, 360, 340, 160, "External SaaS — only public dependency",
           RED, RED_T)
    s.node(1144, 410, 292, 76, "Okta",
           ["authorization server (org or custom)", "groups in token · user MFA"],
           border=RED)

    # ingress path
    s.arrow([(210, 212), (210, 232)])
    s.arrow([(210, 290), (210, 360)])
    s.chip(210, 336, "ZPA tunnel", BLUE)
    s.arrow([(356, 431), (420, 431), (420, 200), (480, 200)])
    s.chip(420, 300, "TLS :443", GREEN, weight="bold")
    s.chip(420, 322, "fingerprint-pinned", GREEN)
    s.arrow([(730, 200), (760, 200)])
    s.chip(745, 148, ":3000", GREEN)
    s.arrow([(605, 240), (605, 320)])
    s.chip(605, 280, ":8080 re-encrypt", GREEN)
    s.arrow([(605, 400), (605, 480)])
    s.chip(605, 440, ":5432 verify-full", GREEN)
    # OIDC flows out
    s.arrow([(730, 352), (900, 352), (900, 430), (1144, 430)])
    s.chip(1000, 418, "OIDC token exchange (TGW egress or proxy)", RED)
    s.arrow([(890, 240), (890, 468), (1144, 468)])
    s.chip(1000, 456, "OAuth code exchange", RED)
    # browser SSO redirect (top corridor)
    s.arrow([(300, 138), (300, 84), (1290, 84), (1290, 360)], dashed=True)
    s.chip(840, 84, "browser SSO redirect (via ZPA) — user MFA at Okta", RED)
    # proxy path (routed below the VPC zone)
    s.arrow([(356, 587), (410, 587), (410, 682), (1250, 682), (1250, 520)],
            dashed=True)
    s.chip(800, 682, "proxy path when mandated", AMBER)

    s.text(36, 716, "No public ingress: the ALB is internal — reachable only "
           "through the ZPA app segment from allow-listed connector source "
           "CIDRs. The Okta issuer is the single internet-bound flow, "
           "port-scoped by security groups.", size=11.5, color=SLATE)
    s.write("01a-access-network-path.svg")


# =====================================================================
# 1b. Workload & data services (split view)
# =====================================================================
def d1b():
    s = SVG(1500, 830, "Workloads & AWS data services",
            "inference, telemetry, secrets and encryption — developer access path is in the access view")

    s.zone(40, 96, 660, 570, "AWS GovCloud VPC", GREEN, GREEN_T,
           "spoke · private subnets only")
    # Gateway task ×2 — reached via the internal ALB (access view); the ADOT
    # collector runs co-resident as a loopback sidecar inside this task.
    s.node(76, 140, 584, 102, "Gateway task — ECS Fargate ×2", [], border=GREEN)
    s.node(92, 170, 250, 52, "claude gateway",
           ["reached via the internal ALB"], border=GREEN, tsize=12)
    s.node(388, 170, 252, 52, "ADOT collector",
           ["loopback OTLP sidecar"], border=GREEN, dashed=True, tsize=12)
    s.arrow([(342, 196), (388, 196)])
    s.chip(365, 232, "loopback :4318", GREEN, size=9.5)
    s.node(76, 300, 260, 90, "RDS PostgreSQL 16",
           ["Multi-AZ · CMK · pgaudit", "app-user login only"],
           border=GREEN, cyl=True)
    s.node(400, 300, 260, 80, "db-admin Lambdas",
           ["bootstrap app DB users +", "rotate secret & roll service"],
           border=GREEN)
    s.node(400, 440, 260, 70, "Grafana",
           ["Okta SSO · usage dashboard"], border=GREEN)
    s.node(76, 440, 260, 80, "Download portal ×2",
           ["reached via the internal ALB", "streams installer ZIP at /portal"],
           border=GREEN)
    s.node(76, 560, 584, 80, "Interface VPC endpoints — each with a resource policy",
           ["bedrock-runtime (2 approved models only) · ecr.api · ecr.dkr · logs",
            "secretsmanager · ecs · aps-workspaces  +  S3 gateway endpoint"],
           border=GREEN)

    s.zone(780, 96, 680, 570, "AWS regional services", VIOLET, VIOLET_T,
           "reached via the endpoints / AWS backbone")
    s.node(810, 150, 300, 66, "Amazon Bedrock",
           ["Claude Opus 4.8 · Sonnet 4.5", "us-gov inference profiles only"],
           border=VIOLET)
    s.node(810, 240, 300, 56, "Managed Prometheus (AMP)",
           ["usage/cost metrics · CMK · 150 d"], border=VIOLET)
    s.node(810, 316, 300, 56, "CloudWatch Logs → Firehose",
           ["activity window (CMK), 14 d"], border=VIOLET)
    s.node(810, 400, 300, 60, "S3: activity archive",
           ["SSE-KMS · 731-day retention"], border=VIOLET, cyl=True)
    s.node(810, 490, 300, 56, "Secrets Manager (CMK)",
           ["all credentials — see §6 inventory"], border=VIOLET)
    s.node(810, 570, 300, 76, "S3: ALB access logs",
           ["SSE-S3 · delivered by the ALB", "(see access view)"],
           border=VIOLET, cyl=True)
    s.node(1150, 150, 286, 120, "KMS CMK  alias/<prefix>",
           ["one customer-managed key,", "rotation enabled — encrypts RDS,",
            "secrets, log groups, activity", "archive, AMP, ECR"],
           border=VIOLET)
    s.node(1150, 358, 286, 56, "S3: portal artifacts (04)",
           ["SSE-KMS · installers + manifest"], border=VIOLET, cyl=True)
    s.node(1150, 447, 286, 56, "CloudWatch: portal-audit (04)",
           ["CMK · 365 d · flag for SIEM"], border=VIOLET)

    # inference (corridor above both zones, entering Bedrock's top edge
    # to the right of the zone labels)
    s.arrow([(280, 140), (280, 86), (1040, 86), (1040, 150)])
    s.chip(640, 86, "inference — SigV4 via bedrock-runtime endpoint · 2 approved models", VIOLET)
    # telemetry sidecar -> AMP (remote_write) and -> CloudWatch (activity stream)
    s.arrow([(660, 188), (772, 188), (772, 255), (810, 255)])
    s.chip(716, 176, "SigV4 remote_write", VIOLET)
    s.arrow([(660, 210), (788, 210), (788, 344), (810, 344)])
    s.chip(715, 226, "activity stream (opt-in)", VIOLET)
    # CloudWatch -> S3 archive
    s.arrow([(960, 372), (960, 400)])
    # Grafana -> AMP
    s.arrow([(660, 470), (770, 470), (770, 292), (810, 292)])
    s.chip(770, 440, "SigV4 query", VIOLET)
    # gateway -> RDS, db-admin -> RDS / Secrets Manager
    s.arrow([(206, 242), (206, 300)])
    s.chip(206, 271, ":5432 verify-full", GREEN)
    s.arrow([(400, 345), (336, 345)])
    s.arrow([(660, 330), (750, 330), (750, 518), (810, 518)])
    s.chip(750, 540, "manage app secret", VIOLET)
    # portal -> S3 artifacts and -> download-audit (right column, via the free
    # bottom band + the zone right margin; risers stay left of the db-admin->
    # secrets line at x750, and land in inter-box gap corridors on the right)
    s.arrow([(200, 520), (200, 528), (690, 528), (690, 386), (1150, 386)])
    s.chip(945, 386, "installers · S3 gw endpoint", VIOLET)
    s.arrow([(244, 520), (244, 542), (712, 542), (712, 475), (1150, 475)])
    s.chip(945, 475, "download-audit (CMK)", VIOLET)

    s.text(36, 726, "The ADOT collector is a co-resident loopback sidecar in the "
           "gateway task (OTLP over 127.0.0.1), so telemetry never crosses the "
           "network in the clear — the former plaintext OTLP hop is gone (C2 "
           "closed). Okta egress and the developer path are on the access view.",
           size=11.5, color=SLATE)
    s.text(36, 748, "Secrets are injected into tasks at launch by ECS "
           "(execution roles hold GetSecretValue + kms:Decrypt on exactly "
           "their own secrets — see §6).", size=11.5, color=SLATE_LT)
    s.text(36, 770, "Download portal (04): serves the installer ZIP from the "
           "CMK-encrypted artifacts bucket via the S3 gateway endpoint and writes a "
           "download-audit stream; its Okta OIDC egress is on the access view.",
           size=11.5, color=SLATE_LT)
    s.write("01b-workloads-data-services.svg")


# =====================================================================
# 7. Security-group topology map
# =====================================================================
def d7():
    s = SVG(1780, 1010, "7. Security groups - who may talk to whom",
            "Every arrow is an explicit SG rule pair (egress on the source + "
            "ingress on the destination). Everything else is denied.")

    s.zone(36, 96, 300, 170, "Corporate network", AMBER, AMBER_T)
    s.node(66, 150, 240, 84, "Developer laptops",
           ["ZPA / VPN networks", "ClientIngressCidr"], border=AMBER)

    s.zone(36, 296, 1330, 660, "Workload VPC - private subnets", GREEN, GREEN_T)

    s.node(96, 356, 250, 92, "Internal ALB", ["SG: alb", "TLS listener :443"],
           border=GREEN)
    s.node(96, 520, 250, 100, "Gateway tasks",
           ["SGs: svc + db-client", "listener :8080 (TLS)",
            "+ ADOT collector sidecar"], border=GREEN)
    s.node(430, 520, 230, 100, "Grafana task",
           ["SG: grafana", "listener :3000 (TLS)"], border=GREEN)
    s.node(740, 520, 230, 100, "ADOT collector - sidecar",
           ["runs inside the gateway task", "loopback 127.0.0.1 - no SG"],
           border=SLATE_LT, dashed=True)
    s.node(1050, 520, 250, 100, "Download portal x2",
           ["SG: portal", "listener :8080 (TLS)"], border=GREEN)
    s.node(140, 700, 206, 96, "db-admin Lambdas",
           ["SGs: db-admin", "+ db-client"], border=GREEN)
    s.node(430, 716, 220, 84, "RDS PostgreSQL",
           ["SG: db - IN 5432", "only from db-client"], border=GREEN, cyl=True)
    s.node(1050, 700, 180, 96, "Admin / build EC2",
           ["SG: admin (param)", "runs deploy scripts"], border=GREEN,
           dashed=True, tsize=12)

    s.node(96, 862, 560, 80, "Interface VPC endpoints (shared SG: endpoint)",
           ["bedrock-runtime | ecr.api | ecr.dkr | logs | secretsmanager | ecs",
            "IN 443 from: svc, db-admin, grafana, portal, admin host"],
           border=VIOLET)
    s.node(740, 862, 230, 80, "AMP endpoint",
           ["SG: amp-endpoint",
            "IN 443: gateway (svc) + grafana"], border=VIOLET)

    s.zone(1426, 96, 332, 400, "External / regional", RED, RED_T)
    s.node(1456, 150, 272, 78, "Okta issuer",
           ["via central egress + Zscaler", "(ALLOW + no-inspect required)"],
           border=RED)
    s.node(1456, 268, 272, 66, "AWS regional APIs",
           ["behind the endpoints below"], border=VIOLET)
    s.node(1456, 372, 272, 96, "Amazon S3 (gateway endpoint)",
           ["route-table entry, no SG;", "policy: ECR layers, CFN responses,",
            "this account's buckets"], border=VIOLET, dashed=True)

    # -- north-south data plane
    s.arrow([(221, 234), (221, 356)], color=AMBER)
    s.chip(221, 300, "443  from ClientIngressCidr", color=AMBER)
    s.arrow([(221, 448), (221, 520)], color=GREEN)
    s.chip(221, 487, "8080  alb->svc", color=GREEN)
    s.arrow([(346, 402), (545, 402), (545, 520)], color=GREEN)
    s.chip(545, 442, "3000  alb->grafana (03)", color=GREEN)
    # alb -> portal (8080): long corridor above row B, into the portal's top
    s.arrow([(346, 372), (1140, 372), (1140, 520)], color=GREEN)
    s.chip(560, 372, "8080  alb->portal (04)", color=GREEN)

    # -- corridors above row B: Okta egress (y470, riser in the gateway/grafana gap).
    # There is no svc->collector OTLP rule any more - the collector is a loopback
    # sidecar in the gateway task, so that hop never touches the network.
    s.arrow([(300, 520), (300, 470), (1010, 470), (1010, 200), (1456, 200)],
            color=RED)
    s.arrow([(560, 520), (560, 470)], color=RED, dashed=True)
    s.chip(660, 458, "443  gateway + grafana -> Okta (OIDC/OAuth)", color=RED)
    # portal -> Okta (its own riser up the right margin into Okta's left edge)
    s.arrow([(1240, 520), (1240, 216), (1456, 216)], color=RED)
    s.chip(1240, 360, "443  portal -> Okta", color=RED)

    # -- database (5432 rides the attached db-client SG)
    s.arrow([(346, 600), (388, 600), (388, 745), (430, 745)], color=SLATE)
    s.arrow([(346, 760), (430, 760)], color=SLATE)
    s.chip(305, 685, "5432  db-client->db", color=SLATE)

    # -- 443 to the shared interface endpoints
    s.arrow([(118, 620), (118, 862)], color=VIOLET)
    s.arrow([(243, 796), (243, 862)], color=VIOLET)
    s.arrow([(460, 620), (460, 668), (400, 668), (400, 862)], color=VIOLET)
    s.arrow([(1060, 796), (1060, 815), (655, 815), (655, 862)], color=VIOLET,
            dashed=True)
    # portal -> shared endpoints (conditional: only when 02 made shared endpoints)
    s.arrow([(1075, 620), (1075, 655), (1010, 655), (1010, 840), (540, 840),
             (540, 862)], color=VIOLET, dashed=True)
    s.chip(790, 840, "443  portal->endpoints (when shared)", color=VIOLET)

    # -- 443 to the AMP endpoint (remote_write now originates in the gateway task
    #    on the svc SG; queries still come from grafana)
    s.arrow([(320, 620), (320, 660), (820, 660), (820, 862)], color=VIOLET)
    s.chip(545, 648, "443  gateway -> AMP (remote_write)", color=VIOLET)
    s.arrow([(660, 585), (700, 585), (700, 640), (905, 640), (905, 862)],
            color=VIOLET)

    s.text(36, 986, "Not shown: every SG's default egress is a 127.0.0.1/32 "
           "placeholder (deny) unless a rule above exists; endpoint ENIs and "
           "RDS initiate nothing. Dashed = optional/conditional (admin-host "
           "param, AMP endpoint toggle, portal's shared-endpoint ingress).",
           size=11, color=SLATE_LT)
    s.write("07-security-groups-map.svg")


# =====================================================================
# 8. Security-group rule inventory
# =====================================================================
def d8():
    s = SVG(1560, 940, "8. Security-group rule inventory",
            "Every rule in the deployment, by group. IN = ingress, OUT = "
            "egress. Groups not listed here do not exist.")

    def card(x, y, name, stack, rules, border=GREEN, note=""):
        h = 64 + 15 * len(rules) + (16 if note else 0)
        s.node(x, y, 470, h, name, [], border=border)
        s.chip(x + 470 - 46, y + 16, stack, color=SLATE_LT, size=9.5)
        for i, r in enumerate(rules):
            s.text(x + 18, y + 44 + i * 15, r, size=10.8, color=SLATE)
        if note:
            s.text(x + 18, y + 48 + len(rules) * 15, note, size=10,
                   color=SLATE_LT)

    c1, c2, c3 = 40, 545, 1050
    card(c1, 96, "alb  (internal ALB)", "02", [
        "IN   443   from ClientIngressCidr (developer networks)",
        "OUT  8080  to svc (forward + health checks)",
        "OUT  3000  to grafana  (rule added by 03)",
        "OUT  8080  to portal   (rule added by 04)"])
    card(c1, 268, "svc  (gateway tasks)", "02", [
        "IN   8080       from alb",
        "OUT  443        0.0.0.0/0 (Okta, AWS APIs via endpoints/egress)",
        "OUT  proxy port 0.0.0.0/0 (only when HttpsProxyUrl set)",
        "OUT  443        to amp-endpoint (remote_write; rule added by 03)"],
        note="also carries db-client (5432) + telemetry sidecar (loopback)")
    card(c1, 458, "db-admin  (bootstrap + rotation Lambdas)", "02", [
        "IN   none",
        "OUT  443  0.0.0.0/0 (Secrets Manager, ECS via endpoints)"],
        note="also carries db-client (below) for 5432")
    card(c1, 606, "endpoint  (shared, all 02 interface endpoints)", "02", [
        "IN   443  from svc, db-admin",
        "IN   443  from grafana              (rule added by 03)",
        "IN   443  from portal               (rule added by 04, when shared)",
        "IN   443  from AdminClientSecurityGroupId (optional param)",
        "OUT  none (endpoint ENIs never initiate)"])
    card(c1, 760, "portal  (download portal task)", "04", [
        "IN   8080  from alb (path rule /portal, /portal/*)",
        "OUT  443   0.0.0.0/0 (Okta, S3, CloudWatch, ECR, Secrets Mgr)",
        "OUT  proxy port 0.0.0.0/0 (only when HttpsProxyUrl set)"])

    card(c2, 96, "db-client  (attach to reach the DB)", "01", [
        "IN   none",
        "OUT  5432  to db"],
        note="attached to: gateway tasks, both db-admin Lambdas")
    card(c2, 244, "db  (RDS instance)", "01", [
        "IN   5432  from db-client",
        "OUT  none"])
    # No collector SG: the ADOT collector runs as a loopback sidecar in the
    # gateway task (svc SG), so its former SG and OTLP ingress no longer exist.
    card(c2, 376, "grafana  (dashboard task)", "03", [
        "IN   3000  from alb (path rule /grafana)",
        "OUT  443   0.0.0.0/0 (AMP queries, Okta OAuth, ECR)"])
    card(c2, 524, "amp-endpoint  (aps-workspaces endpoint)", "03", [
        "IN   443  from svc (gateway task - remote_write)",
        "IN   443  from grafana (queries)",
        "OUT  none"])

    s.node(c3, 96, 470, 300, "Cross-stack rule writers", [], border=SLATE)
    for i, ln in enumerate([
            "03 adds rules to SGs it imports from 02:",
            "  - AlbToGrafanaEgress:         alb OUT 3000 -> grafana",
            "  - GatewayToAmpEndpointEgress: svc OUT 443 -> amp-endpoint",
            "  - GrafanaToEndpointsIngress:  endpoint IN 443",
            "04 adds rules to SGs it imports from 02:",
            "  - AlbToPortalEgress:        alb  OUT 8080 -> portal",
            "  - PortalToEndpointsIngress: endpoint IN 443 (when shared)",
            "02 param AdminClientSecurityGroupId:",
            "  - AdminToEndpointsIngress:     endpoint IN 443",
            "",
            "Deploy order matters: imported SGs must exist first",
            "(02 before 03/04); CREATE_SUPPORTING_ENDPOINTS",
            "must match across the deploys."]):
        s.text(c3 + 18, 130 + i * 17, ln, size=10.8, color=SLATE)
    s.node(c3, 430, 470, 170, "Reading the map", [], border=SLATE)
    for i, ln in enumerate([
            "A connection works only when BOTH ends agree:",
            "an egress rule on the source SG and an ingress",
            "rule on the destination SG. Every SG here is",
            "default-deny; the 127.0.0.1/32 egress entries in",
            "the templates are deliberate no-op placeholders",
            "that suppress the default allow-all egress."]):
        s.text(c3 + 18, 464 + i * 17, ln, size=10.8, color=SLATE)

    s.text(36, 908, "Resolved (was accepted risk C2): the gateway->collector OTLP "
           "hop is no longer on the network - the ADOT collector runs as a "
           "co-resident sidecar in the gateway task, reached over loopback "
           "(127.0.0.1:4318). No SG rule exists or is needed for it.",
           size=11, color=SLATE_LT)
    s.write("08-security-group-rules.svg")


# =====================================================================
# 9. Access-control layers beyond SGs
# =====================================================================
def d9():
    s = SVG(1560, 700, "9. Layered access control - what each layer stops",
            "One request traced through every gate: a gateway task reading "
            "its DB secret through the Secrets Manager endpoint.")

    gates = [
        ("1. Source SG egress", "svc allows 443 out", GREEN),
        ("2. Endpoint SG ingress", "only the named SGs\n(svc, db-admin, ...)",
         GREEN),
        ("3. Endpoint policy", "only THIS account's\nsecret ARNs", VIOLET),
        ("4. IAM (execution role)", "only this task's own\nsecret ARNs",
         VIOLET),
        ("5. KMS key policy", "decrypt via the CMK,\ngrantees only", VIOLET),
    ]
    x = 250
    s.node(50, 150, 170, 90, "Gateway task", ["wants its", "db-app secret"],
           border=GREEN)
    for i, (t, sub, col) in enumerate(gates):
        gx = x + i * 230
        s.node(gx, 128, 200, 134, t, [], border=col)
        for j, ln in enumerate(sub.split("\n")):
            s.text(gx + 100, 172 + j * 15, ln, size=10.5, color=SLATE,
                   anchor="middle")
        s.arrow([(gx - 60 if i else 220, 195), (gx, 195)], color=GREEN)
    s.arrow([(x + 4 * 230 + 200, 195), (1510, 195)], color=GREEN)
    s.node(1385, 150, 125, 90, "Secret", ["decrypted", "value"], border=GREEN,
           cyl=True)

    s.text(50, 320, "Why the layers are not redundant - each stops a "
           "different failure:", size=13, color=INK, weight="bold")
    rows = [
        ("Compromised box in another subnet, ANY credentials",
         "stopped at layer 2 - its SG is not in the endpoint SG ingress "
         "(network path denied before authn)"),
        ("Compromised workload using ATTACKER-OWNED AWS credentials "
         "(exfiltration through the private endpoint)",
         "stopped at layer 3 - the endpoint policy allows only this "
         "account's resources; your IAM cannot judge foreign principals"),
        ("Legitimate workload asking for a secret it does not own",
         "stopped at layer 4 - execution roles enumerate exact ARNs "
         "(gateway task cannot read the master secret: no task injects it)"),
        ("Principal with a stolen ciphertext or over-broad S3/API access",
         "stopped at layer 5 - the CMK key policy grants decrypt only to "
         "the roles that need it"),
    ]
    y = 348
    for attack, stop in rows:
        s.text(66, y, "- " + attack, size=11.5, color=RED, weight="bold")
        s.text(84, y + 17, stop, size=11, color=SLATE)
        y += 48

    s.node(50, 552, 1460, 104,
           "Non-SG enforcement points elsewhere in the deployment", [],
           border=VIOLET)
    for i, ln in enumerate([
            "bedrock-runtime endpoint policy: pinned to the TWO configured "
            "model IDs + their inference profiles (not anthropic.*)   |   "
            "ecs endpoint: NO policy (GovCloud unsupported) - IAM-side "
            "scoping covers it",
            "S3 gateway endpoint policy: ECR layer bucket + CloudFormation "
            "custom-resource response buckets + this account's buckets only",
            "ALB access-log bucket policy: exactly two writers (ELB "
            "log-delivery service principal + legacy regional ELB account)   "
            "|   AMP endpoint policy: this workspace's ARN only",
            "Okta egress: Zscaler policy must ALLOW + not inspect the issuer "
            "FQDN for server-originated traffic (org prerequisite - see the "
            "networking request)"]):
        s.text(70, 586 + i * 18, ln, size=10.8, color=SLATE)
    s.write("09-access-control-layers.svg")


if __name__ == "__main__":
    d1(); d1a(); d1b(); d2(); d3(); d4(); d5(); d6(); d7(); d8(); d9()
