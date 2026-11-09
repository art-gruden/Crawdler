"""Extract company rows from saved Firmendatenbank Sachsen result pages into an import CSV."""
import csv, glob, re, sys
from bs4 import BeautifulSoup

src_dir, out = sys.argv[1], sys.argv[2]
rows = {}
for f in sorted(glob.glob(f"{src_dir}/*Firmendatenbank*.html")):
    s = BeautifulSoup(open(f, encoding="utf-8", errors="replace").read(), "lxml")
    label = s.select_one(".selectric .label")
    category = (label.get_text(" ", strip=True).split("(")[0].strip() if label else "")
    for tr in s.select("table.table tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 4:
            continue
        a = tds[0].find("a", href=True)
        if not a or "/detail/" not in a["href"]:
            continue
        link = a["href"].split("?")[0]
        m = re.search(r"/detail/(\d+)/", link)
        fid = m.group(1) if m else ""
        name = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        codes, texts = [], []
        for r in tds[1].select("div.row"):
            cols = r.find_all("div", recursive=False)
            if len(cols) >= 2:
                c = cols[0].get_text(strip=True)
                if re.fullmatch(r"\d{5}", c):
                    c = f"{c[:2]}.{c[2:4]}.{c[4]}"
                codes.append(c)
                texts.append(cols[1].get_text(" ", strip=True))
        parts = [p.strip() for p in tds[2].get_text("\n", strip=True).split("\n") if p.strip()]
        plz = parts[0] if parts and re.fullmatch(r"\d{5}", parts[0]) else ""
        ort = " ".join(parts[1:]) if plz else " ".join(parts)
        web = tds[3].select_one("a.icon-monitor[href]")
        website = web["href"].strip() if web else ""
        key = fid or link
        rows[key] = {
            "Firmen-ID": fid, "Firmenname": name, "Internet": website, "PLZ": plz, "Ort": ort,
            "Technologiefelder": category, "WZ 2008": "; ".join(codes), "Branche": "; ".join(texts),
            "Link": link,
        }
fields = ["Firmen-ID", "Firmenname", "Internet", "PLZ", "Ort", "Technologiefelder", "WZ 2008", "Branche", "Link"]
with open(out, "w", encoding="utf-8-sig", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=fields, delimiter=";")
    w.writeheader()
    for r in sorted(rows.values(), key=lambda r: r["Firmenname"].lower()):
        w.writerow(r)
print(len(rows))
