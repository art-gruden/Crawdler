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
    "de": r"deutsch(?:kenntnisse|e|en)?|german",
    "en": r"englisch(?:kenntnisse|e|en)?|english",
    "fr": r"französisch|french",
    "pl": r"polnisch|polish",
    "cs": r"tschechisch|czech",
    "ru": r"russisch|russian",
    "uk": r"ukrainisch|ukrainian",
}
_LEVEL = (
    r"(?P<cefr>[abc][12](?:\s*[-/+]\s*[abc]?[12])?)|"
    r"(?P<word>muttersprach\w*|native|verhandlungssicher\w*|fließend\w*|fliessend\w*|fluent\w*|sehr gut\w*|"
    r"exzellent\w*|excellent|proficient|gut\w*|good|solide\w*|grundkenntnisse|basic|business[- ]fluent|"
    r"wünschenswert|von vorteil|nice to have|plus)"
)
_LEVEL_MAP = [
    ("C2", r"muttersprach|native"),
    ("C1", r"verhandlungssicher|fließend|fliessend|fluent|exzellent|excellent|business[- ]fluent|proficient"),
    ("B2", r"sehr gut"),
    ("B1", r"\bgut|good|solide"),
    ("A2", r"grundkenntnisse|basic"),
]


def find_languages(text: str) -> list[dict]:
    """[{"language": "de", "level": "C1", "required": true, "raw": "..."}]"""
    out: dict[str, dict] = {}
    for line in re.split(r"[\n.;]", text[:40_000]):
        for code, rx in _LANGS.items():
            m = re.search(rf"(?<!\w)({rx})(?!\w)", line, re.I)
            if not m:
                continue
            # skip "deutsche Firma", "Germany" as location, "German market" etc.
            window = line[max(0, m.start() - 60) : m.end() + 60]
            if not re.search(
                r"kenntnis|sprach|language|spoken|written|wort und schrift|skills|level|niveau|"
                r"[abc][12]\b|fließend|fluent|verhandlungssicher|muttersprach|native|gut|good|"
                r"proficien|kommunik|communicat|plus|vorteil|wünschenswert|nice to have|required|erforderlich",
                window,
                re.I,
            ):
                continue
            optional = bool(
                re.search(
                    r"wünschenswert|von vorteil|nice to have|\bplus\b|optional|ideally|idealerweise", window, re.I
                )
            )
            level = _nearest_level(line, m.start(), m.end())
            prev = out.get(code)
            if prev is None or (level and not prev.get("level")):
                out[code] = {"language": code, "level": level, "required": not optional, "raw": window.strip()[:160]}
    return list(out.values())


def _nearest_level(line: str, start: int, end: int) -> str | None:
    """Level that belongs to *this* language mention (closest; CEFR right after wins)."""
    best = None
    for lm in re.finditer(_LEVEL, line, re.I):
        if lm.group("cefr"):
            after = lm.start() - end
            before = start - lm.end()
            if 0 <= after <= 30 or 0 <= before <= 3:
                return re.sub(r"\s", "", lm.group("cefr")).upper()[:5]
            continue
        dist = start - lm.end() if lm.end() <= start else lm.start() - end
        if lm.end() <= start and dist > 25 or lm.start() >= end and dist > 20:
            continue
        word = lm.group("word").lower()
        lvl = next((lv for lv, rxl in _LEVEL_MAP if re.search(rxl, word)), None)
        if lvl and (best is None or dist < best[0]):
            best = (dist, lvl)
    return best[1] if best else None
