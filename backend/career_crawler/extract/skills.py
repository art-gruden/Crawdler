"""Technology-skill and language-requirement detection (curated dictionary, word boundaries)."""

from __future__ import annotations

import re

# canonical name → (category, regex alternatives). Kept explicit to avoid false positives
# such as "Go" in German text or "R" anywhere.
_SKILLS: dict[str, tuple[str, str]] = {
    "Kubernetes": ("devops", r"kubernetes|k8s"),
    "Docker": ("devops", r"docker"),
    "Podman": ("devops", r"podman"),
    "Helm": ("devops", r"helm(?:\s+charts?)?(?=\W)"),
    "OpenShift": ("devops", r"openshift"),
    "Terraform": ("iac", r"terraform"),
    "OpenTofu": ("iac", r"opentofu"),
    "Pulumi": ("iac", r"pulumi"),
    "Ansible": ("iac", r"ansible"),
    "Puppet": ("iac", r"puppet"),
    "Chef": ("iac", r"chef(?=\s*(?:,|/|und|and|oder|or|\)|infra))"),
    "CloudFormation": ("iac", r"cloudformation"),
    "ArgoCD": ("cicd", r"argo\s?cd"),
    "FluxCD": ("cicd", r"flux\s?cd"),
    "GitOps": ("cicd", r"gitops"),
    "Jenkins": ("cicd", r"jenkins"),
    "GitLab CI": ("cicd", r"gitlab(?:[- ]ci)?"),
    "GitHub Actions": ("cicd", r"github actions"),
    "CI/CD": ("cicd", r"ci\s?/\s?cd"),
    "AWS": ("cloud", r"aws|amazon web services"),
    "Azure": ("cloud", r"azure"),
    "GCP": ("cloud", r"gcp|google cloud"),
    "OpenStack": ("cloud", r"openstack"),
    "Prometheus": ("observability", r"prometheus"),
    "Grafana": ("observability", r"grafana"),
    "ELK": ("observability", r"elk(?:\s+stack)?|elasticsearch|kibana|logstash"),
    "OpenTelemetry": ("observability", r"opentelemetry|otel"),
    "Datadog": ("observability", r"datadog"),
    "Linux": ("os", r"linux|debian|ubuntu|rhel|red\s?hat enterprise|centos|suse"),
    "Windows Server": ("os", r"windows server"),
    "Bash": ("language", r"bash|shell[- ]?script(?:ing)?"),
    "Python": ("language", r"python"),
    "Go": ("language", r"golang|go(?=\s*(?:,|;|/|\)|\n|$|\(golang\)|\((?:m|w|f|d)/)|\s(?:und|and|oder|or)\s)"),
    "Java": ("language", r"java(?!\s*script)"),
    "Kotlin": ("language", r"kotlin"),
    "C#": ("language", r"c#|\.net|dotnet"),
    "C++": ("language", r"c\+\+"),
    "Rust": ("language", r"rust(?=\W)"),
    "JavaScript": ("language", r"javascript"),
    "TypeScript": ("language", r"typescript"),
    "PHP": ("language", r"php"),
    "SQL": ("database", r"sql(?!ite)"),
    "PostgreSQL": ("database", r"postgres(?:ql)?"),
    "MySQL": ("database", r"mysql|mariadb"),
    "MongoDB": ("database", r"mongo\s?db"),
    "Redis": ("database", r"redis"),
    "Kafka": ("database", r"kafka"),
    "RabbitMQ": ("database", r"rabbitmq"),
    "Nginx": ("network", r"nginx"),
    "Istio": ("network", r"istio"),
    "Networking": ("network", r"tcp/ip|dns|bgp|vpn|firewall"),
    "Vault": ("security", r"hashicorp vault|vault(?=\s*(?:,|/|\)|und|and))"),
    "React": ("frontend", r"react(?:\.?js)?"),
    "Angular": ("frontend", r"angular"),
    "Vue": ("frontend", r"vue(?:\.?js)?"),
    "SAP": ("enterprise", r"sap(?:\s+(?:s/4\s?hana|erp|abap|basis))?"),
    "Embedded C": ("embedded", r"embedded c|eingebettete systeme|mikrocontroller|microcontroller"),
    "FPGA": ("embedded", r"fpga|vhdl|verilog"),
    "Machine Learning": ("data", r"machine learning|maschinelles lernen|deep learning|pytorch|tensorflow"),
    "Spark": ("data", r"apache spark|pyspark"),
    "SRE": ("practice", r"site reliability|\bsre\b"),
    "DevOps": ("practice", r"devops"),
    "ITIL": ("practice", r"itil"),
    "Scrum": ("practice", r"scrum|agile|agil(?:e|en) methoden"),
}
_COMPILED = [(name, cat, re.compile(rf"(?<![\w.+#-])(?:{rx})(?![\w+#])", re.I)) for name, (cat, rx) in _SKILLS.items()]


def find_skills(text: str) -> list[tuple[str, str]]:
    text = text[:60_000]
    return [(name, cat) for name, cat, rx in _COMPILED if rx.search(text)]


_LANGS = {
    "de": r"deutsch(?:kenntnisse)?|deutsche[rn]? sprache|german|allemand|tedesco|schweizerdeutsch|swiss german",
    "en": r"englisch(?:kenntnisse)?|english|anglais|inglese",
    "fr": r"franz(?:ö|oe|o)sisch(?:kenntnisse)?|french|fran(?:ç|c)ais|francese",
    "it": r"italienisch(?:kenntnisse)?|italian|italiano|italien",
    "es": r"spanisch(?:kenntnisse)?|spanish|espa(?:ñ|n)ol|spagnolo",
    "nl": r"niederl(?:ä|ae|a)ndisch|dutch|holl(?:ä|ae|a)ndisch",
    "pt": r"portugiesisch|portuguese",
    "pl": r"polnisch|polish",
    "cs": r"tschechisch|czech",
    "ru": r"russisch|russian",
    "uk": r"ukrainisch|ukrainian",
    "tr": r"t(?:ü|ue|u)rkisch|turkish",
    "ar": r"arabisch|arabic",
}
CEFR_ORDER = {"A1": 1, "A2": 2, "B1": 3, "B2": 4, "C1": 5, "C2": 6}
_CEFR = r"[ABC][12]"
_CEFR_RANGE = re.compile(rf"(?<![\w/])({_CEFR})(?:\s*(?:[-–/]|bis|to|or|oder)\s*({_CEFR}))?(?![\w])")
# Descriptive words are *not* CEFR levels: they never produce a level, only a descriptor.
_DESCRIPTORS = [
    ("native", r"muttersprach\w*|native|langue maternelle|madrelingua|bilingual|zweisprachig"),
    ("fluent", r"verhandlungssicher\w*|flie(?:ß|ss)end\w*|fluent\w*|business[- ]fluent|courant\w*|fluente"),
    ("advanced", r"sehr gut\w*|exzellent\w*|excellent\w*|ausgezeichnet\w*|proficient|très bon\w*|ottim\w*|perfekt\w*"),
    ("good", r"gut(?:e|en)?\b|good|solid\w*|bon(?:ne)?s?\b|buon\w*"),
    ("basic", r"grundkenntnisse|grundlagen|basic|notions|conoscenza di base|elementar\w*"),
]
_DESC_RX = [(name, re.compile(rx, re.I)) for name, rx in _DESCRIPTORS]
_PREFERRED = re.compile(
    r"wünschenswert|wuenschenswert|von vorteil|vorteilhaft|nice[- ]to[- ]have|\bplus\b|pluspunkt|optional|ideally|"
    r"idealerweise|desirable|preferred|bevorzugt|souhait|apprezzat|gerne gesehen|ist ein vorteil|wären? (?:ein )?vorteil",
    re.I,
)
_REQUIRED = re.compile(
    r"erforderlich|notwendig|vorausgesetzt|voraussetzung|zwingend|unbedingt|\bmust\b|required|essential|mandatory|"
    r"obligatoire|requis|richiest|sicher(?:e|er|en)?\b|\bmind(?:estens|\.)?\b|at least|minimum",
    re.I,
)
_CONTEXT = re.compile(
    r"kenntnis|sprach|language|spoken|written|wort und schrift|skills|level|niveau|[abc][12]\b|fließend|flie(?:ß|ss)end|"
    r"fluent|verhandlungssicher|muttersprach|native|\bgut|good|proficien|kommunik|communicat|plus|vorteil|"
    r"wünschenswert|nice to have|required|erforderlich|langue|lingua|parl|sprech|speak|konversation|conversation",
    re.I,
)


def find_languages(text: str, requirement_lines: list[str] | None = None) -> list[dict]:
    """Language requirements found in ``text``.

    Each item: ``language`` (ISO 639-1), ``level`` (CEFR A1–C2 *only when written explicitly*, else
    None), ``requirement`` (required | preferred | unknown), ``required`` (legacy bool: requirement is
    "required"), ``descriptor`` (native | fluent | advanced | good | basic | None), ``evidence``
    (the matched wording), ``confidence``. A range such as "B2–C1" yields its lower bound.
    "Fließende Deutschkenntnisse" is German, required, descriptor ``fluent`` and level None.
    """
    req_set = {ln.strip().lower() for ln in (requirement_lines or [])}
    out: dict[str, dict] = {}
    text = re.sub(r"\b(mind|min|ca|bzw|ggf|inkl|evtl|ggfs|z\.\s?B|u\.a|d\.h)\.", r"\1", text[:40_000], flags=re.I)
    for line in re.split(r"[\n.;]", text):
        for code, rx in _LANGS.items():
            for m in re.finditer(rf"(?<!\w)({rx})(?!\w)", line, re.I):
                window = line[max(0, m.start() - 60) : m.end() + 60]
                if not _CONTEXT.search(window):
                    continue
                # markers belong to this language's clause ("Französisch fließend, Italienisch ein Plus")
                lo = max(line.rfind(",", 0, m.start()), line.rfind(";", 0, m.start())) + 1
                hits = [i for i in (line.find(",", m.end()), line.find(";", m.end())) if i >= 0]
                hi = min(hits) if hits else len(line)
                clause = line[lo:hi]
                cs, ce = m.start() - lo, m.end() - lo
                level, level_text = _explicit_level(clause, cs, ce)
                descriptor = _descriptor(clause, cs, ce)
                preferred = bool(_PREFERRED.search(clause))
                in_req = line.strip().lstrip("-•·*▪► ").strip().lower() in req_set
                required = (
                    not preferred
                    and bool(_REQUIRED.search(clause) or in_req or descriptor in ("native", "fluent", "advanced", "good") or level)
                )
                requirement = "preferred" if preferred else "required" if required else "unknown"
                conf = 0.95 if level else 0.8 if descriptor else 0.6
                if requirement == "unknown":
                    conf = min(conf, 0.5)
                evidence = window.strip()[:160]
                item = {
                    "language": code,
                    "level": level,
                    "requirement": requirement,
                    "required": requirement == "required",
                    "descriptor": descriptor,
                    "evidence": evidence,
                    "raw": evidence,
                    "confidence": round(conf, 2),
                }
                if level_text and "-" in level_text:
                    item["level_range"] = level_text
                prev = out.get(code)
                if prev is None or _better(item, prev):
                    out[code] = item
    return list(out.values())


_REQ_RANK = {"required": 2, "unknown": 1, "preferred": 0}


def _better(new: dict, old: dict) -> bool:
    """Keep the most informative mention: explicit level first, then a firmer requirement."""
    if bool(new["level"]) != bool(old["level"]):
        return bool(new["level"])
    if _REQ_RANK[new["requirement"]] != _REQ_RANK[old["requirement"]]:
        return _REQ_RANK[new["requirement"]] > _REQ_RANK[old["requirement"]]
    return bool(new["descriptor"]) and not old["descriptor"]


def _explicit_level(line: str, start: int, end: int) -> tuple[str | None, str | None]:
    """CEFR written near *this* language mention (right after it, or a few characters before)."""
    best = None
    for lm in _CEFR_RANGE.finditer(line):
        after = lm.start() - end
        before = start - lm.end()
        dist = after if after >= 0 else (before if before >= 0 else None)
        if dist is None:
            continue
        limit = 30 if after >= 0 else 6
        if dist > limit:
            continue
        a, b = lm.group(1).upper(), (lm.group(2) or "").upper()
        lvl = min((a, b), key=lambda x: CEFR_ORDER[x]) if b else a
        if best is None or dist < best[0]:
            best = (dist, lvl, f"{a}-{b}" if b else a)
    return (best[1], best[2]) if best else (None, None)


def _descriptor(line: str, start: int, end: int) -> str | None:
    best = None
    for name, rx in _DESC_RX:
        for lm in rx.finditer(line):
            dist = start - lm.end() if lm.end() <= start else lm.start() - end
            if dist < 0:
                continue
            limit = 25 if lm.end() <= start else 22
            if dist > limit:
                continue
            if best is None or dist < best[0]:
                best = (dist, name)
    return best[1] if best else None


SKILL_ALIASES = {
    "k8s": "Kubernetes",
    "golang": "Go",
    "postgres": "PostgreSQL",
    "js": "JavaScript",
    "ts": "TypeScript",
    "cicd": "CI/CD",
    "gitlab": "GitLab CI",
    "dotnet": "C#",
    ".net": "C#",
    "ml": "Machine Learning",
    "mongo": "MongoDB",
}
SKILL_BY_LOWER = {name.lower(): name for name in _SKILLS}


def canonical_skill(name: str) -> str | None:
    """Canonical skill name for user input (case-insensitive, a few common aliases), else None."""
    key = name.strip().lower()
    return SKILL_BY_LOWER.get(key) or SKILL_ALIASES.get(key)


def skill_category(name: str) -> str | None:
    entry = _SKILLS.get(name)
    return entry[0] if entry else None
