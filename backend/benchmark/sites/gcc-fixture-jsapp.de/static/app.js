fetch('/api/jobs.json').then(r => r.json()).then(jobs => {
  const app = document.getElementById('app');
  app.innerHTML = '<h1>Karriere</h1><h2>Offene Stellen</h2><ul>' + jobs.map(j =>
    `<li><a href="/karriere/${j.slug}">${j.title}</a> – ${j.city}</li>`).join('') + '</ul>';
});