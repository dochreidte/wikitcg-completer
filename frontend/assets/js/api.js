/** Thin wrappers over the dashboard's JSON endpoints. */

export async function getJSON(url) {
  return (await fetch(url)).json();
}

export async function postJSON(url, body) {
  const opt = { method: 'POST' };
  if (body !== undefined) {
    opt.headers = { 'Content-Type': 'application/json' };
    opt.body = JSON.stringify(body);
  }
  return (await fetch(url, opt)).json();
}

export async function delJSON(url) {
  return (await fetch(url, { method: 'DELETE' })).json();
}
