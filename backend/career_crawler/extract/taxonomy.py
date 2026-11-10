"""Specialization taxonomy, seniority and classification (DE/EN, some FR/IT).

The taxonomy is data: ids are stable and stored in ``jobs.specialization_id`` /
``job_specializations``; the same rows are seeded into the ``specializations`` table.

Classification principles
-------------------------
* The job *title* decides. A title pattern is strong evidence; skills and description hints are weak
  and can never make a specialization primary on their own while some other specialization
  matches the title ("Backend Developer … Kubernetes" is backend, not DevOps).
* Without any title match, at least three distinct skills of one specialization are needed and the
  confidence is capped at 0.4.
* The original title is never changed; seniority comes from the title only and is ``UNKNOWN``
  rather than guessed.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

TAXONOMY_VERSION = 1

# --- data ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Spec:
    id: int
    slug: str
    name_en: str
    name_de: str
    group: int | None  # id of the parent group, None for groups
    is_it: bool = False
    title: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    hints: tuple[str, ...] = ()
    sort: int = 0


def _s(id, slug, en, de, group, titles="", skills="", hints="", is_it=False):  # noqa: A002
    return Spec(
        id,
        slug,
        en,
        de,
        group,
        is_it,
        tuple(t for t in titles.split("§") if t),
        tuple(x for x in skills.split(",") if x),
        tuple(h for h in hints.split("§") if h),
        id,
    )


GROUPS = [
    Spec(1, "information-technology", "Information Technology", "Informationstechnologie", None, True),
    Spec(2, "engineering", "Engineering & Technical", "Technik & Ingenieurwesen", None),
    Spec(3, "business", "Business & Management", "Wirtschaft & Management", None),
    Spec(4, "sales-marketing", "Sales & Marketing", "Vertrieb & Marketing", None),
    Spec(5, "operations", "Operations, Logistics & Production", "Betrieb, Logistik & Produktion", None),
    Spec(6, "health-social-education", "Health, Social & Education", "Gesundheit, Soziales & Bildung", None),
    Spec(7, "services-trades", "Services & Trades", "Dienstleistung & Handwerk", None),
    Spec(8, "finance-legal-admin", "Finance, Legal & Administration", "Finanzen, Recht & Verwaltung", None),
    Spec(9, "science-environment", "Science & Environment", "Wissenschaft & Umwelt", None),
]

LEAVES: list[Spec] = [
    # --- IT ---------------------------------------------------------------------------------
    _s(10, "backend-development", "Backend Development", "Backend-Entwicklung", 1,
       r"back[- ]?end|server[- ]?side|(java|python|php|golang|go|rust|c#|\.net|node(?:\.?js)?|ruby|scala|kotlin) ?(developer|engineer|entwickl|programmier)",
       "Python,Java,Go,Kotlin,C#,PHP,Rust,SQL,PostgreSQL,MySQL,MongoDB,Redis,Kafka,RabbitMQ", is_it=True),
    _s(11, "frontend-development", "Frontend Development", "Frontend-Entwicklung", 1,
       r"front[- ]?end|web ?(developer|entwickl)|ui ?(developer|entwickl)|(react|angular|vue|typescript|javascript) ?(developer|engineer|entwickl)",
       "React,Angular,Vue,JavaScript,TypeScript", is_it=True),
    _s(12, "fullstack-development", "Full-Stack Development", "Full-Stack-Entwicklung", 1, r"full[- ]?stack", "", is_it=True),
    _s(13, "mobile-development", "Mobile Development", "Mobile-Entwicklung", 1,
       r"\bios\b|android|mobile ?(app )?(developer|entwickl|engineer)|app[- ]?entwickl|flutter|react native|swift ?(developer|entwickl)",
       "Kotlin", is_it=True),
    _s(14, "software-engineering", "Software Engineering", "Softwareentwicklung", 1,
       r"software ?(engineer|entwickl|developer|ingenieur|programmier)|softwareentwickl|programmierer|(?<![a-z])entwickler|(?<![a-z])developer|application developer|anwendungsentwickl",
       "Python,Java,C#,C++,Go,Rust,Kotlin,TypeScript,JavaScript", is_it=True),
    _s(15, "devops-platform", "DevOps & Platform Engineering", "DevOps & Platform Engineering", 1,
       r"dev ?ops|devsecops|platform ?(engineer|ingenieur)|plattform ?(engineer|ingenieur)|infrastruktur ?(engineer|ingenieur)|infrastructure ?engineer|release ?(engineer|manager)|build ?engineer|ci ?/ ?cd|gitops|cloud[- ]native|kubernetes",
       "Kubernetes,Docker,Podman,Helm,OpenShift,Terraform,OpenTofu,Pulumi,Ansible,ArgoCD,FluxCD,GitOps,Jenkins,GitLab CI,GitHub Actions,CI/CD,Prometheus,Grafana",
       "ci/cd pipeline", is_it=True),
    _s(16, "site-reliability", "Site Reliability Engineering", "Site Reliability Engineering", 1,
       r"\bsre\b|site reliability|reliability engineer", "Prometheus,Grafana,Kubernetes,SRE", is_it=True),
    _s(17, "cloud-engineering", "Cloud Engineering", "Cloud Engineering", 1,
       r"cloud ?(engineer|architect|ingenieur|architekt|consultant|administrator|specialist|developer)|(aws|azure|gcp) ?(engineer|architect|specialist|administrator)",
       "AWS,Azure,GCP,OpenStack,CloudFormation", is_it=True),
    _s(18, "data-engineering", "Data Engineering", "Data Engineering", 1,
       r"data ?engineer|dateningenieur|\betl\b|big ?data|data ?platform|analytics ?engineer|\bdwh\b|data ?warehouse",
       "Spark,Kafka,SQL,Python", is_it=True),
    _s(19, "data-science-ml", "Data Science & Machine Learning", "Data Science & Machine Learning", 1,
       r"data ?scien|machine ?learning|\bml ?engineer|ki[- ]?(entwickl|engineer|spezialist)|\bai ?(engineer|research|specialist)|deep ?learning|\bnlp\b|computer ?vision|mlops|kunstliche intelligenz|llm",
       "Machine Learning,Spark,Python", is_it=True),
    _s(20, "data-analytics-bi", "Data Analytics & BI", "Data Analytics & BI", 1,
       r"data ?analy|\bbi\b ?(analyst|entwickl|developer|consultant)|business intelligence|power ?bi|tableau|reporting ?analy|datenanaly",
       "SQL", is_it=True),
    _s(21, "qa-testing", "QA & Test Engineering", "QA & Testautomatisierung", 1,
       r"\bqa\b|quality assurance|test ?(engineer|automat|manager|analyst|ingenieur)|\btester\b|\bsdet\b|testautomatisier|software ?test",
       "", is_it=True),
    _s(22, "embedded-systems", "Embedded Systems", "Embedded Systems", 1,
       r"embedded|firmware|\bfpga\b|vhdl|autosar|mikrocontroller|microcontroller",
       "Embedded C,FPGA", is_it=True),
    _s(23, "it-security", "IT Security", "IT-Sicherheit", 1,
       r"it[- ]?sicherheit|informationssicherheit|cyber|security ?(engineer|analyst|architect|consultant|specialist|officer|expert)|penetration|pentest|\bsoc\b|\bciso\b|\bisms\b|information security",
       "Vault", is_it=True),
    _s(24, "it-support", "IT Support & Service Desk", "IT-Support & Service Desk", 1,
       r"it[- ]?support|help ?desk|service ?desk|\b(1st|2nd|3rd|first|second|third)[- ]level|it[- ]?techniker|desktop support|user support|anwenderbetreuung|it[- ]?betreuer",
       "", is_it=True),
    _s(25, "system-administration", "System Administration", "Systemadministration", 1,
       r"system ?administrat|sysadmin|it[- ]?administrat|(linux|windows|netzwerk|network|server) ?administrat|systemingenieur|system ?engineer|systemtechniker|systemintegrat|it[- ]?infrastruktur",
       "Linux,Windows Server", is_it=True),
    _s(26, "network-engineering", "Network Engineering", "Netzwerktechnik", 1,
       r"network ?(engineer|architect|specialist)|netzwerk ?(engineer|ingenieur|techniker|spezialist|architekt)|netzwerkadministr|telekommunikation",
       "Networking", is_it=True),
    _s(27, "database-administration", "Database Administration", "Datenbankadministration", 1,
       r"\bdba\b|database ?(admin|administrator|engineer|developer)|datenbank ?(administrat|entwickl|spezialist)", "PostgreSQL,MySQL,SQL", is_it=True),
    _s(28, "erp-sap", "ERP & SAP", "ERP & SAP", 1,
       r"\bsap\b|\berp\b|\babap\b|s/4|navision|dynamics 365|business central|salesforce|crm ?(consultant|berater|administrator)",
       "SAP", is_it=True),
    _s(29, "software-architecture", "Software Architecture", "Softwarearchitektur", 1,
       r"(software|solution|solutions|enterprise|it|system|technical|technology)[- ]?(architect|architekt)|losungsarchitekt|softwarearchitekt", "", is_it=True),
    _s(30, "product-management", "Product Management", "Produktmanagement", 1,
       r"product ?(manager|owner|lead)|produkt ?(manager|verantwortlich|owner)|produktmanager|\bpo\b", "", is_it=True),
    _s(31, "it-project-management", "IT Project & Delivery Management", "IT-Projektmanagement", 1,
       r"(it|software|technical|digital)[- ]?(project|projekt)[- ]?(manager|leiter|leitung)|scrum ?master|agile ?coach|delivery ?manager|program(me)? ?manager|programmmanager|\bpmo\b",
       "Scrum,ITIL", is_it=True),
    _s(32, "ux-ui-design", "UX / UI Design", "UX / UI Design", 1,
       r"\bux\b|\bui ?(design|designer)|user experience|interaction design|product designer|webdesign", "", is_it=True),
    _s(33, "game-development", "Game Development", "Spieleentwicklung", 1,
       r"game ?(developer|designer|programmer|artist)|spiele ?entwickl|unity ?(developer|entwickl)|unreal", "", is_it=True),
    _s(34, "it-management", "IT & Engineering Management", "IT- & Entwicklungsleitung", 1,
       r"\bcto\b|\bcio\b|head of (engineering|it|technology|development|platform)|it[- ]?leiter|leiter (it|entwicklung|software)|engineering ?manager|it[- ]?manager|vp engineering|abteilungsleiter it|teamleiter (it|entwicklung)",
       "", is_it=True),
    _s(35, "it-consulting", "IT Consulting", "IT-Beratung", 1,
       r"it[- ]?(berater|consultant|beratung)|technology consultant|technical consultant|solution consultant|digital consultant|technologieberat", "", is_it=True),
    # --- engineering ------------------------------------------------------------------------
    _s(40, "mechanical-engineering", "Mechanical Engineering", "Maschinenbau", 2,
       r"maschinenbau|mechanical|konstrukteur|konstruktion|\bcad\b|mechatronik|fahrzeugtechnik|entwicklungsingenieur|mechanik"),
    _s(41, "electrical-engineering", "Electrical Engineering", "Elektrotechnik", 2,
       r"elektro|electrical|elektrotechnik|energietechnik|schaltschrank|hochspannung|leistungselektronik|power electronics"),
    _s(42, "automation-engineering", "Automation & Robotics", "Automatisierung & Robotik", 2,
       r"automatisier|automation|\bsps\b|\bplc\b|robotik|robotics|steuerungstechnik|leittechnik"),
    _s(43, "civil-construction", "Civil Engineering & Construction", "Bau & Bauingenieurwesen", 2,
       r"bauingenieur|bauleiter|bauleitung|civil engineer|statiker|tragwerk|hochbau|tiefbau|bauzeichner|polier|construction ?manager|\btga\b|haustechnik|building services|architekt(?!ur)|bauwesen"),
    _s(44, "process-chemical-engineering", "Process & Chemical Engineering", "Verfahrens- & Chemietechnik", 2,
       r"verfahrenstechn|process engineer|chemieingenieur|chemical engineer|chemielaborant|biotechnolog|pharmatechn"),
    _s(45, "quality-management", "Quality Management", "Qualitätsmanagement", 2,
       r"qualitats ?(management|manager|sicherung|beauftragt|ingenieur|prufer|kontroll)|quality ?(manager|engineer|inspector|control)|\bqm[- ]|auditor"),
    # --- operations -------------------------------------------------------------------------
    _s(46, "production-manufacturing", "Production & Manufacturing", "Produktion & Fertigung", 5,
       r"produktion|fertigung|montage|maschinenbediener|maschinenfuhrer|industriemechaniker|zerspan|\bcnc\b|schweisser|lackierer|production ?(worker|operator|manager)|werker|mechatroniker|anlagenbediener|schichtleiter"),
    _s(47, "maintenance-service-technician", "Maintenance & Service Technicians", "Instandhaltung & Servicetechnik", 5,
       r"instandhalt|wartungs|servicetechniker|service technician|field ?service|kundendiensttechniker|anlagenmechaniker|betriebstechnik"),
    _s(48, "logistics-supply-chain", "Logistics & Supply Chain", "Logistik & Supply Chain", 5,
       r"logistik|supply ?chain|\blager|disponent|spedition|versand|warehouse|lagerist|kommissionier|staplerfahrer"),
    _s(49, "procurement", "Procurement & Purchasing", "Einkauf & Beschaffung", 5,
       r"einkauf|procurement|purchasing|\bbuyer\b|beschaffung|sourcing"),
    _s(50, "transport-driving", "Transport & Driving", "Transport & Fahrdienst", 5,
       r"kraftfahrer|berufskraftfahrer|\blkw\b|fahrer|\bdriver\b|zusteller|kurier|chauffeur|busfahrer|lokfuhrer"),
    # --- business ---------------------------------------------------------------------------
    _s(55, "sales", "Sales", "Vertrieb", 4,
       r"vertrieb|\bsales\b|account ?(manager|executive)|verkaufer|\bverkauf\b|business development|key account|aussendienst|innendienst|kundenberater"),
    _s(56, "marketing-communications", "Marketing & Communications", "Marketing & Kommunikation", 4,
       r"marketing|\bseo\b|\bsem\b|social media|content ?(manager|marketing|creator)|\bbrand\b|\bpr\b|public relations|kommunikation|redakteur|copywriter|\bgrowth\b|communications"),
    _s(57, "customer-service", "Customer Service & Support", "Kundenservice & Support", 5,
       r"customer ?(service|support|success)|kundenservice|kundenbetreuer|kundendienst|call ?center|helpline|kundensupport|serviceberater|service agent"),
    _s(58, "hr-recruiting", "HR & Recruiting", "Personal & Recruiting", 3,
       r"\bhr\b|human resources|personal(referent|sachbearbeiter|leiter|berater|entwicklung|wesen)|recruit|talent acquisition|people (partner|operations|&)|lohnbuchhalt|payroll"),
    _s(59, "finance-accounting", "Finance & Accounting", "Finanzen & Buchhaltung", 8,
       r"buchhalt|accountant|accounting|controller|controlling|finanz|finance|bilanz|steuer(?!ung)|\btax\b|treasury|kreditor|debitor|wirtschaftsprufer"),
    _s(60, "legal-compliance", "Legal & Compliance", "Recht & Compliance", 8,
       r"jurist|\blegal\b|rechtsanwalt|anwalt|compliance|syndikus|paralegal|rechtsreferent|datenschutz"),
    _s(61, "administration-office", "Administration & Office", "Verwaltung & Büro", 8,
       r"sachbearbeiter|assistenz|\bassistant\b|sekretar|office ?manager|empfang|back ?office|verwaltung|buroleiter|buromanagement|kaufmann|kauffrau|burokaufmann|buro"),
    _s(62, "management-executive", "General Management", "Geschäftsführung & Management", 3,
       r"geschaftsfuhrer|managing director|\bceo\b|\bcoo\b|\bcfo\b|vorstand|bereichsleiter|prokurist|niederlassungsleiter|general manager"),
    _s(63, "consulting-strategy", "Consulting & Strategy", "Beratung & Strategie", 3,
       r"unternehmensberat|strategy consult|management consult|managementberat|\bberater\b|\bconsultant\b"),
    _s(64, "project-management", "Project Management", "Projektmanagement", 3,
       r"projektleiter|project ?manager|projektmanager|projektkoordinator|project coordinator"),
    _s(65, "business-analysis", "Business Analysis", "Business Analyse", 3,
       r"business ?analyst|requirements ?engineer|anforderungs|fachlicher analyst|prozessmanager|process ?manager"),
    # --- health / social / education --------------------------------------------------------
    _s(70, "healthcare", "Healthcare & Nursing", "Gesundheit & Pflege", 6,
       r"pflege|krankenschwester|\bnurse\b|\barzt\b|arztin|medizin|therapeut|physiotherap|\bmfa\b|altenpfleg|gesundheits|krankenpfleg|apotheke|pharmazeut|zahnmedizin|zahnarzt|hebamme|rettungs|pharmacist|infirmi|infermier"),
    _s(71, "social-work", "Social Work & Childcare", "Soziale Arbeit & Erziehung", 6,
       r"sozialarbeiter|sozialpadagog|erzieher|\bkita\b|pedagog|heilerziehung|jugendhilfe|sozialassistent|betreuungskraft"),
    _s(72, "education-training", "Education & Training", "Bildung & Ausbildung", 6,
       r"lehrer|teacher|dozent|\btrainer\b|ausbilder|lecturer|professor|schulleit|lehrkraft|\btutor\b|enseignant|insegnante"),
    # --- services / trades ------------------------------------------------------------------
    _s(75, "skilled-trades", "Skilled Trades", "Handwerk", 7,
       r"handwerk|elektriker|installateur|sanitar|heizung|klempner|tischler|schreiner|maurer|dachdecker|\bmaler\b|zimmerer|\bkfz\b|mechaniker|schlosser|fliesenleger|gartner|landschaftsbau|metallbauer|bodenleger|glaser"),
    _s(76, "hospitality-gastronomy", "Hospitality & Gastronomy", "Gastronomie & Hotellerie", 7,
       r"\bkoch\b|\bkoechin\b|kuche|gastronom|restaurant|\bhotel|servicekraft|kellner|barista|barkeeper|rezeption|housekeeping|commis|sous chef|chef de partie|hotelfach|backer|konditor|fleischer|metzger"),
    _s(77, "retail", "Retail", "Einzelhandel", 7,
       r"einzelhandel|verkaufsberater|\bfiliale|filialleit|kassierer|\bretail\b|store manager|marktleiter|verkaufsstellenleiter|handelsfach|verkaufer(?:in)? im"),
    _s(78, "facility-cleaning-security", "Facility, Cleaning & Security", "Facility, Reinigung & Sicherheitsdienst", 7,
       r"reinigung|\bcleaner\b|gebaudereinig|hausmeister|facility|sicherheitsdienst|security guard|wachmann|objektschutz|portier"),
    _s(79, "creative-media", "Creative, Design & Media", "Kreativ, Design & Medien", 4,
       r"grafikdesign|graphic design|mediengestalt|\bvideo|fotograf|redaktion|journalist|content creator|art director|illustrator|motion design|3d artist|texter|layouter"),
    _s(80, "science-research", "Science & Research", "Wissenschaft & Forschung", 9,
       r"wissenschaftlich|researcher|forscher|postdoc|doktorand|\bphd\b|research scientist|laborant|biolog|chemiker|physiker|mathematiker|\blabor"),
    _s(81, "environment-sustainability", "Environment & Sustainability", "Umwelt & Nachhaltigkeit", 9,
       r"landwirt|agrar|forst|umwelt|environmental|nachhaltigkeit|sustainability|\besg\b|klima"),
    _s(82, "financial-services", "Banking, Insurance & Real Estate", "Bank, Versicherung & Immobilien", 8,
       r"immobilien|versicherung|\bbank\b|bankkauf|kreditberater|finanzberater|vermogensberater|makler|underwriter|insurance"),
    _s(83, "energy-utilities", "Energy & Utilities", "Energie & Versorgung", 2,
       r"energie|photovoltaik|\bsolar|windkraft|energy|netzbetrieb|wasserstoff|hydrogen"),
]

SPECIALIZATIONS: list[Spec] = [*GROUPS, *LEAVES]
BY_ID = {s.id: s for s in SPECIALIZATIONS}
BY_SLUG = {s.slug: s for s in SPECIALIZATIONS}

SENIORITIES = ("INTERN", "JUNIOR", "MID", "SENIOR", "LEAD", "EXECUTIVE", "UNKNOWN")


# --- matching -----------------------------------------------------------------------------------


def soft_fold(text: str) -> str:
    t = unicodedata.normalize("NFKD", (text or "").lower().replace("ß", "ss"))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t)


_TITLE_RX = {s.id: [re.compile(p) for p in s.title] for s in LEAVES}
_HINT_RX = {s.id: [re.compile(p) for p in s.hints] for s in LEAVES}

_SENIORITY = [
    (
        "INTERN",
        re.compile(
            r"praktikum|praktikant|\bintern\b|internship|werkstudent|working student|auszubildende|\bazubi\b|"
            r"ausbildung|duales studium|stagiaire|\bstage\b|apprenti|tirocin|apprendista|abschlussarbeit|"
            r"masterarbeit|bachelorarbeit|\bthesis\b"
        ),
    ),
    (
        "JUNIOR",
        re.compile(
            r"\bjunior\b|\bjr\.?\b|berufseinsteiger|einsteiger|entry[- ]level|absolvent|\bgraduate\b|\btrainee\b|"
            r"nachwuchs|young professional"
        ),
    ),
    (
        "EXECUTIVE",
        re.compile(
            r"geschaftsfuhrer|managing director|\bceo\b|\bcto\b|\bcfo\b|\bcoo\b|\bcio\b|\bciso\b|\bvp\b|"
            r"vice president|vorstand|direktor|\bdirector\b|prokurist|general manager"
        ),
    ),
    (
        "LEAD",
        re.compile(
            r"\blead\b|head of|\bprincipal\b|staff (engineer|developer)|team ?lead|teamleiter|teamleitung|"
            r"abteilungsleiter|gruppenleiter|bereichsleiter|supervisor|\bleiter(in)?\b|\bleitung\b|\bchef\b"
        ),
    ),
    ("SENIOR", re.compile(r"\bsenior\b|\bsr\.?\b|erfahren|experienced|\bexperte\b|\bexpert\b")),
    ("MID", re.compile(r"mid[- ]?level|mid[- ]?senior|intermediate|berufserfahren")),
]


@dataclass
class Classification:
    primary_id: int | None = None
    confidence: float = 0.0
    secondary: list[tuple[int, float]] = field(default_factory=list)
    seniority: str = "UNKNOWN"
    evidence: dict = field(default_factory=dict)


def seniority(title: str) -> str:
    t = soft_fold(title)
    for name, rx in _SENIORITY:
        if rx.search(t):
            return name
    return "UNKNOWN"


def classify(title: str, text: str = "", skills: list[str] | tuple[str, ...] = ()) -> Classification:
    """Specialization (primary + secondary), confidence and seniority for one posting."""
    t = soft_fold(title)
    body = soft_fold(text[:3000])
    skill_set = set(skills)
    title_score: dict[int, float] = {}
    soft: dict[int, float] = {}
    for s in LEAVES:
        hits = sum(1 for rx in _TITLE_RX[s.id] if rx.search(t))
        if hits:
            title_score[s.id] = 6.0 + 1.0 * (hits - 1)
        overlap = len(skill_set.intersection(s.skills))
        weak = min(overlap, 3) * 1.0 + min(sum(1 for rx in _HINT_RX[s.id] if rx.search(body)), 2) * 0.7
        if weak:
            soft[s.id] = weak
    out = Classification(seniority=seniority(title))
    if title_score:
        # more specific titles beat the generic "software engineering" bucket
        scores = {i: sc + min(soft.get(i, 0.0), 3.0) for i, sc in title_score.items()}
        generic = {14, 63, 64, 61}
        specific = [i for i in scores if i not in generic]
        if specific and 14 in scores:
            scores[14] -= 2.0
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        top_id, top = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        conf = 0.7 + min(0.2, (top - 6.0) * 0.05) - (0.1 if top - second < 1.0 and len(ranked) > 1 else 0.0)
        out.primary_id = top_id
        out.confidence = round(max(0.5, min(0.95, conf)), 2)
        out.secondary = [(i, round(min(0.9, 0.5 + 0.05 * (sc - 6.0)), 2)) for i, sc in ranked[1:4] if sc >= 6.0]
        out.evidence = {"basis": "title", "title_score": round(top, 1)}
        return out
    # no title evidence: needs three distinct skills of one specialization, confidence is capped
    best = None
    for s in LEAVES:
        overlap = skill_set.intersection(s.skills)
        if len(overlap) >= 3:
            sc = len(overlap) + soft.get(s.id, 0.0)
            if best is None or sc > best[1]:
                best = (s, sc, sorted(overlap))
    if best:
        out.primary_id = best[0].id
        out.confidence = round(min(0.4, 0.1 * len(best[2])), 2)
        out.evidence = {"basis": "skills", "skills": best[2][:5]}
    return out


def public_spec(spec_id: int | None) -> dict | None:
    s = BY_ID.get(spec_id) if spec_id else None
    if not s:
        return None
    parent = BY_ID.get(s.group) if s.group else None
    return {
        "id": s.id,
        "slug": s.slug,
        "name": s.name_en,
        "name_de": s.name_de,
        "group": parent.slug if parent else None,
        "is_it": s.is_it,
    }


def expand(slugs_or_ids: list[str]) -> list[int]:
    """Specialization ids for a filter; selecting a group includes all its members."""
    out: set[int] = set()
    for v in slugs_or_ids:
        s = BY_SLUG.get(v) if not str(v).isdigit() else BY_ID.get(int(v))
        if s is None:
            raise KeyError(f"unknown specialization '{v}'")
        if s.group is None:
            out.update(x.id for x in LEAVES if x.group == s.id)
            out.add(s.id)
        else:
            out.add(s.id)
    return sorted(out)
