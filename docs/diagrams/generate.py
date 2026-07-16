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

    def text(self, x, y, s, size=11, color=SLATE, weight="normal",
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
             tsize=13, cyl=False, dashed=False):
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
    s = SVG(1500, 1010, "System architecture & trust boundaries",
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

    s.zone(432, 96, 640, 700, "AWS GovCloud VPC", GREEN, GREEN_T,
           "spoke · private subnets only · no IGW/NAT required")
    s.node(470, 150, 250, 76, "Internal ALB  :443",
           ["enterprise-CA cert (ACM import)", "IPv4-only · deletion-protected",
            "stack-policy locked"], border=GREEN)
    s.node(790, 150, 250, 76, "Grafana  :3000 TLS",
           ["per-task self-signed cert", "Okta SSO only, no local login"],
           border=GREEN)
    s.node(470, 292, 250, 76, "Gateway — ECS Fargate ×2",
           ["claude gateway (pinned binary)", "TLS listener :8080, per-task cert"],
           border=GREEN)
    s.node(790, 292, 250, 62, "ADOT collector ×2",
           ["OTLP :4317 / :4318"], border=GREEN)
    s.node(470, 434, 250, 84, "RDS PostgreSQL 16",
           ["Multi-AZ · CMK · pgaudit", "app-user login only",
            "stack-policy locked"], border=GREEN, cyl=True)
    s.node(790, 420, 250, 76, "db-admin Lambdas",
           ["bootstrap app DB users +", "rotate secret & roll service"],
           border=GREEN)
    s.node(470, 566, 570, 96, "Interface VPC endpoints — each with a resource policy",
           ["bedrock-runtime (2 approved models only) · ecr.api · ecr.dkr · logs",
            "secretsmanager · ecs · aps-workspaces  +  S3 gateway endpoint"],
           border=GREEN)
    s.node(470, 692, 570, 76, "KMS CMK  alias/<prefix>",
           ["one customer-managed key: RDS, secrets, log groups,",
            "activity archive, AMP, ECR (rotation enabled)"], border=GREEN)

    s.zone(1124, 96, 336, 564, "AWS regional services", VIOLET, VIOLET_T,
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

    s.zone(1124, 700, 336, 120, "External SaaS — the only public dependency",
           RED, RED_T)
    s.node(1148, 744, 288, 58, "Okta",
           ["authorization server (org or custom)", "returns groups in token"], border=RED)

    # ---- flows
    s.arrow([(210, 212), (210, 232)])
    s.arrow([(210, 290), (210, 360)])
    s.chip(210, 336, "ZPA tunnel", BLUE)
    s.arrow([(356, 431), (415, 431), (415, 188), (470, 188)])
    s.chip(415, 240, "TLS :443", GREEN, weight="bold")
    s.arrow([(720, 188), (790, 188)])
    s.chip(755, 176, ":3000", GREEN)
    s.arrow([(595, 226), (595, 292)])
    s.chip(595, 262, ":8080 re-encrypt", GREEN)
    s.arrow([(595, 368), (595, 434)])
    s.chip(595, 404, ":5432 verify-full", GREEN)
    # gateway -> collector (the one plaintext hop)
    s.arrow([(720, 330), (790, 330)])
    s.chip(757, 318, ":4318", RED)
    s.chip(757, 378, "plaintext · SG-scoped", RED, border=RED)
    # Grafana -> AMP (straight into the top row)
    s.arrow([(1040, 178), (1148, 178)])
    s.chip(1094, 166, "SigV4 query", VIOLET)
    # gateway -> Bedrock (straight corridor between the box rows)
    s.arrow([(720, 300), (756, 300), (756, 258), (1148, 258)])
    s.chip(900, 258, "inference — SigV4", VIOLET)
    # collector -> AMP
    s.arrow([(1010, 354), (1010, 378), (1120, 378), (1120, 206), (1148, 206)])
    s.chip(1058, 378, "SigV4 remote_write", VIOLET)
    # collector -> CloudWatch (activity stream)
    s.arrow([(915, 354), (915, 396), (1148, 396)])
    s.chip(985, 398, "activity stream (opt-in)", VIOLET)
    # db-admin -> RDS and -> Secrets Manager
    s.arrow([(790, 470), (720, 470)])
    s.arrow([(915, 496), (915, 520), (1136, 520), (1136, 338), (1148, 338)])
    s.chip(1024, 520, "manage app secret", VIOLET)
    # gateway -> Okta (down the VPC's clear left margin, out the bottom)
    s.arrow([(470, 330), (452, 330), (452, 830), (1180, 830), (1180, 802)])
    s.chip(800, 830, "OIDC login + token exchange (TGW egress or proxy)", RED)
    # Grafana -> Okta
    s.arrow([(940, 226), (940, 274), (1096, 274), (1096, 760), (1148, 760)])
    s.chip(1096, 690, "OAuth code exchange", RED)
    # optional proxy path
    s.arrow([(356, 587), (410, 587), (410, 862), (1240, 862), (1240, 820)],
            dashed=True)
    s.chip(700, 862, "proxy path when mandated", AMBER)

    s.text(36, 980, "Boundary facts: no public ingress (internal ALB behind ZPA); "
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
         "PLAINTEXT :4318 OTLP — SG-to-SG scoped · accepted risk, see §10",
         "ADOT collector", RED, True),
        (7, "ADOT collector", GREEN,
         "TLS :443 + SigV4 — Prometheus remote_write",
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
    ]
    top, step, rh = 120, 50, 38
    s = SVG(1400, top + step * len(hops) + 76,
            "Network flows, ports & TLS state — every hop in the system",
            "one row per flow; row 6 is the only unencrypted hop")
    for i, (n, src, sc, proto, dst, dc, red) in enumerate(hops):
        y = top + i * step
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
    fy = top + step * len(hops) + 30
    s.text(48, fy, "TLS termination points: developer→ALB terminates on the "
           "enterprise cert (developers pin its fingerprint); ALB→task hops "
           "terminate on per-task ephemeral certs (ALBs do not validate "
           "target certs; keys never leave the task).", size=11.5, color=SLATE)
    s.text(48, fy + 20, "All AWS-service hops (5, 7, 8) also carry SigV4 "
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
    s.node(380, 180, 260, 96, "Gateway",
           ["stamps user.id · user.email ·", "user.groups from the Okta JWT",
            "onto every export"], border=GREEN)
    s.node(740, 180, 260, 90, "ADOT collector",
           ["drops session.id (cardinality)", "promotes team / cost_center",
            "to metric labels"], border=GREEN)

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

    s.arrow([(288, 188), (380, 210)])
    s.chip(330, 176, "OTLP via gateway FQDN", BLUE)
    s.arrow([(288, 291), (334, 291), (334, 244), (380, 244)])
    s.arrow([(640, 214), (740, 214)])
    s.chip(690, 202, "metrics", GREEN)
    s.arrow([(640, 250), (700, 250), (700, 462), (764, 462)])
    s.chip(700, 366, "activity records (only when enabled)", RED)
    s.arrow([(1000, 200), (1104, 185)])
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
           ["AMP (CMK) · collector ×2", "Grafana + Okta SSO",
            "activity archive chain"], border=GREEN)
    s.node(1290, 150, 170, 90, "02 re-run",
           ["picks up", "OBSERVABILITY_OTLP_URL,", "starts forwarding"],
           border=GREEN)

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
    s.text(1250, 284, "OtlpForwardUrl", size=10.5, color=SLATE,
           anchor="middle")
    s.add(f'<path d="M1250 272 V 208" stroke="{CHIP_BORDER}" stroke-width="1" '
          f'stroke-dasharray="2 3" fill="none"/>')

    s.text(48, 500, "Locks a reviewer should know: the RDS storage CMK is fixed at creation "
           "(plus 01↔02 export locks) — a day-one decision; 03 must be deleted before "
           "02 replacement-updates; the ALB and Database carry stack policies denying "
           "Update:Replace / Update:Delete.", size=11.5, color=SLATE)
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
    s.node(76, 150, 260, 80, "Gateway — ECS Fargate ×2",
           ["reached via the internal ALB", "(see access view)"], border=GREEN)
    s.node(400, 150, 260, 70, "ADOT collector ×2",
           ["OTLP :4317 / :4318"], border=GREEN)
    s.node(76, 300, 260, 90, "RDS PostgreSQL 16",
           ["Multi-AZ · CMK · pgaudit", "app-user login only"],
           border=GREEN, cyl=True)
    s.node(400, 300, 260, 80, "db-admin Lambdas",
           ["bootstrap app DB users +", "rotate secret & roll service"],
           border=GREEN)
    s.node(400, 440, 260, 70, "Grafana",
           ["Okta SSO · usage dashboard"], border=GREEN)
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

    # inference (corridor above both zones, entering Bedrock's top edge
    # to the right of the zone labels)
    s.arrow([(280, 150), (280, 86), (1040, 86), (1040, 150)])
    s.chip(640, 86, "inference — SigV4 via bedrock-runtime endpoint · 2 approved models", VIOLET)
    # gateway -> collector (plaintext)
    s.arrow([(336, 190), (400, 190)])
    s.chip(368, 166, ":4318", RED)
    s.chip(368, 246, "plaintext · SG-scoped", RED, border=RED)
    # collector -> AMP (enter top) and -> CloudWatch
    s.arrow([(660, 185), (740, 185), (740, 240)])
    s.chip(700, 185, "SigV4 remote_write", VIOLET)
    s.arrow([(660, 205), (788, 205), (788, 344), (810, 344)])
    s.chip(715, 216, "activity stream (opt-in)", VIOLET)
    # CloudWatch -> S3 archive
    s.arrow([(960, 372), (960, 400)])
    # Grafana -> AMP
    s.arrow([(660, 470), (770, 470), (770, 292), (810, 292)])
    s.chip(770, 440, "SigV4 query", VIOLET)
    # gateway -> RDS, db-admin -> RDS / Secrets Manager
    s.arrow([(206, 230), (206, 300)])
    s.chip(206, 266, ":5432 verify-full", GREEN)
    s.arrow([(400, 345), (336, 345)])
    s.arrow([(660, 330), (750, 330), (750, 518), (810, 518)])
    s.chip(750, 540, "manage app secret", VIOLET)

    s.text(36, 726, "The gateway→collector OTLP hop is the only unencrypted "
           "flow (SG-to-SG scoped — accepted risk §10). Okta egress and the "
           "developer path are on the access view.", size=11.5, color=SLATE)
    s.text(36, 748, "Secrets are injected into tasks at launch by ECS "
           "(execution roles hold GetSecretValue + kms:Decrypt on exactly "
           "their own secrets — see §6).", size=11.5, color=SLATE_LT)
    s.write("01b-workloads-data-services.svg")


if __name__ == "__main__":
    d1(); d1a(); d1b(); d2(); d3(); d4(); d5(); d6()
