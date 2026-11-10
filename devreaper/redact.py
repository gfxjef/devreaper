"""Hide secrets in command lines before they are shown, logged or stored. Tools are often run with tokens as
arguments (`npx vercel --token …`, `TOKEN=… node`, `https://user:pass@host`); the panel, the kill log and the
database must never keep them."""
import re

MASK = "***"
_WORDS = r"token|secret|password|passwd|pwd|api[-_]?key|apikey|auth|credential|private[-_]?key|access[-_]?key|bearer"
# --token xyz, --token=xyz, -token xyz, --github-token "x y"
FLAG = re.compile(rf"(?i)(--?[\w.-]*(?:{_WORDS})[\w.-]*)(=|\s+)(\"[^\"]*\"|'[^']*'|[^\s\"']+)")
# TOKEN=xyz, set API_KEY=xyz, $env:VERCEL_TOKEN="xyz"
ASSIGN = re.compile(rf"(?i)(\b[\w.]*(?:{_WORDS})[\w.]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s\"']+)")
# scheme://user:password@host
URL_CREDS = re.compile(r"(\b[a-z][\w+.-]*://[^/\s:@]+):[^/\s@]+@", re.I)
# Authorization: Bearer xyz
BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
# Well-known token shapes, even as plain arguments.
KNOWN = re.compile(r"\b(ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|glpat-|sk-ant-|sk-proj-|sk-|sk_live_|rk_live_|"
                   r"xox[abprs]-|AKIA|ASIA|AIza|npm_|vercel_|hf_)[A-Za-z0-9_\-]{8,}")


def redact(cmd: str) -> str:
    if not cmd:
        return cmd or ""
    cmd = URL_CREDS.sub(rf"\1:{MASK}@", cmd)
    cmd = BEARER.sub(rf"\1 {MASK}", cmd)
    cmd = FLAG.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", cmd)
    cmd = ASSIGN.sub(lambda m: f"{m.group(1)}={MASK}", cmd)
    return KNOWN.sub(lambda m: m.group(1) + MASK, cmd)
