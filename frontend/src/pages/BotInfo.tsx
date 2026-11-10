// Public information page for website owners: https://<host>/bot
// Needs no login and calls only the public endpoint /api/public/bot (no internal settings).
import { useEffect, useState } from "react";

interface BotInfoData {
  name: string;
  user_agent: string;
  robots_token: string;
  contact_email: string | null;
  per_domain_delay_s: number;
  max_crawl_delay_s: number;
}

export function isBotPage(): boolean {
  return /^\/bot\/?$/.test(window.location.pathname);
}

export default function BotInfo() {
  const [d, setD] = useState<BotInfoData | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    document.title = "Germany Career Crawler – information for website owners";
    fetch("/api/public/bot", { credentials: "omit" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then(setD)
      .catch(() => setFailed(true));
  }, []);

  const token = d?.robots_token ?? "GermanyCareerCrawler";
  const delay = d ? d.per_domain_delay_s : null;
  const mail = d?.contact_email ?? null;
  const robotsBlock = `User-agent: ${token}\nDisallow: /`;
  const robotsSlow = `User-agent: ${token}\nCrawl-delay: 30`;

  const contact = mail ? (
    <a href={`mailto:${mail}`}>{mail}</a>
  ) : (
    <em>
      {failed ? "(could not be loaded)" : "(the operator has not published a contact address yet)"}
    </em>
  );

  return (
    <main className="bot-page">
      <h1>Germany Career Crawler</h1>
      <p className="hint">Information for website owners · Informationen für Websitebetreiber</p>

      <section lang="en">
        <h2>Who we are and why we visit your site</h2>
        <p>
          Germany Career Crawler is a program that discovers <strong>publicly accessible career pages and job
          advertisements of companies in Germany</strong> so that they can be listed and searched in one place.
        </p>

        <h2>What we collect</h2>
        <ul>
          <li>the company's website address,</li>
          <li>public job titles, descriptions, locations, publication dates and the URLs of the job ads.</li>
        </ul>
        <p>We only read pages that anyone can open in a browser. We never log in, fill in forms, or open non-public areas.</p>

        <h2>How we crawl responsibly</h2>
        <ul>
          <li>
            We read your <code>/robots.txt</code> first and obey it. Our product token is <code>{token}</code>. Pages and data
            requests that it disallows are not fetched.
          </li>
          <li>
            Only one request at a time per website{delay !== null && <>, at least {delay} seconds apart</>}. A{" "}
            <code>Crawl-delay</code> in your robots.txt is honoured (values above {d?.max_crawl_delay_s ?? 30} s are treated as
            that value). On <code>429</code>/<code>503</code> responses and <code>Retry-After</code> we slow down or stop.
          </li>
          <li>
            We do not bypass CAPTCHAs, logins, paywalls or bot protection. If a site refuses us, we stop and do not try again
            in a different disguise.
          </li>
          <li>We identify ourselves honestly. Our requests carry this User-Agent:</li>
        </ul>
        <pre>{d?.user_agent ?? "…"}</pre>

        <h2>Reduce our visits or exclude your site</h2>
        <p>The quickest way is your robots.txt. To exclude your whole site:</p>
        <pre>{robotsBlock}</pre>
        <p>To make us slower:</p>
        <pre>{robotsSlow}</pre>
        <p>
          You can also write to us and we will remove your domain or reduce the frequency: {contact}. Please include the domain
          concerned.
        </p>

        <h2>Contact</h2>
        <p>{contact}</p>
      </section>

      <section lang="de" className="lang">
        <h2>Wer wir sind und warum wir Ihre Website besuchen</h2>
        <p>
          Der Germany Career Crawler ist ein Programm, das <strong>öffentlich zugängliche Karriereseiten und Stellenanzeigen
          von Unternehmen in Deutschland</strong> findet, damit sie an einem Ort durchsuchbar sind.
        </p>

        <h2>Was wir erfassen</h2>
        <ul>
          <li>die Internetadresse des Unternehmens,</li>
          <li>öffentliche Stellentitel, Beschreibungen, Orte, Veröffentlichungsdaten und die URLs der Anzeigen.</li>
        </ul>
        <p>
          Wir lesen nur Seiten, die jede Person im Browser öffnen kann. Wir melden uns nirgends an, füllen keine Formulare aus
          und rufen keine nicht öffentlichen Bereiche auf.
        </p>

        <h2>Wie wir verantwortungsvoll crawlen</h2>
        <ul>
          <li>
            Wir lesen zuerst Ihre <code>/robots.txt</code> und befolgen sie (Produkt-Token: <code>{token}</code>). Dort
            untersagte Seiten und Datenabrufe werden nicht geladen.
          </li>
          <li>
            Höchstens eine Anfrage gleichzeitig pro Website{delay !== null && <>, mit mindestens {delay} Sekunden Abstand</>}.
            Ein <code>Crawl-delay</code> wird beachtet. Bei <code>429</code>/<code>503</code> oder <code>Retry-After</code>{" "}
            werden wir langsamer bzw. brechen ab.
          </li>
          <li>
            Wir umgehen weder CAPTCHAs noch Logins, Bezahlschranken oder Bot-Schutz. Wird uns der Zugriff verweigert, hören wir
            auf.
          </li>
          <li>Wir geben uns offen zu erkennen: Unsere Anfragen tragen den oben genannten User-Agent.</li>
        </ul>

        <h2>Besuche reduzieren oder Ihre Website ausschließen</h2>
        <p>
          Am schnellsten geht es über Ihre robots.txt (siehe Beispiele oben). Alternativ schreiben Sie uns: {contact}. Bitte
          nennen Sie die betroffene Domain; wir entfernen sie oder verringern die Abrufhäufigkeit.
        </p>
      </section>

      <noscript>
        <p>
          This page needs JavaScript to show the contact address. Product token: GermanyCareerCrawler. Exclude us with a
          robots.txt group “User-agent: GermanyCareerCrawler / Disallow: /”.
        </p>
      </noscript>
    </main>
  );
}
