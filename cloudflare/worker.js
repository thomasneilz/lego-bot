// Bot LEGO : réponses instantanées aux commandes Telegram (Cloudflare Worker, offre gratuite)
//
// Le Worker reçoit tes messages Telegram (webhook) et répond lui-même en 1 seconde :
//   /achat, /stock (bouton ✅ Vendu), /deals [magasin], /aide, restauration d'une sauvegarde.
// Ton stock et les deals récents sont dans la base D1 « DB ».
// Le bot GitHub (toutes les 15 min) envoie ici les nouveaux deals (POST /deals)
// et déclenche le contrôle quotidien « moment de vendre » (POST /daily).
//
// Réglages Cloudflare (Settings → Variables and Secrets) :
//   BOT_TOKEN (secret), CHAT_ID (texte), SHARED_SECRET (secret, même valeur que sur GitHub)
// Liaison (Settings → Bindings) : base D1, variable « DB ».

const REPO_RAW = "https://raw.githubusercontent.com/thomasneilz/lego-bot/main/watchlist.csv";
const SELL_TARGET = 50; // alerte « moment de vendre » à +50 %
const FLAGS = { GB: "🇬🇧", FR: "🇫🇷", DE: "🇩🇪", ES: "🇪🇸", IT: "🇮🇹", NL: "🇳🇱", AT: "🇦🇹" };

const HELP =
  "🧱 <b>Commandes du bot LEGO</b>\n" +
  "/achat 75415 48.99 : enregistre un achat. Virgule ou point, € ou £ avant ou après.\n" +
  "   Options : x2 (quantité), nom du magasin. Ex. : /achat 75399 35,99£ x2 amazon\n" +
  "/stock : ton stock, sa valeur et ta plus-value (bouton ✅ Vendu)\n" +
  "/deals : meilleures affaires des 7 derniers jours\n" +
  "/deals fnac : seulement ce magasin (leclerc, amazon, argos…)\n" +
  "/aide : réaffiche et réépingle ce message\n" +
  "<i>Réponse instantanée.</i>";

// ---------------------------------------------------------------- utilitaires
const esc = (t) => String(t ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const sym = (cur) => (cur === "GBP" ? "£" : "€");
const toFloat = (s) => {
  s = String(s).replace(/\s/g, "");
  const m = s.match(/^(.*?)[.,](\d{1,2})$/);
  return m ? parseFloat(m[1].replace(/[.,]/g, "") + "." + m[2]) : parseFloat(s.replace(/[.,]/g, ""));
};
const today = () => new Date().toISOString().slice(0, 10);

async function tg(env, method, params) {
  const r = await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
  return r.json();
}

const send = (env, text, extra = {}) =>
  tg(env, "sendMessage", { chat_id: env.CHAT_ID, text, parse_mode: "HTML", disable_web_page_preview: true, ...extra });

async function schema(env) {
  await env.DB.batch([
    env.DB.prepare(`CREATE TABLE IF NOT EXISTS stock (id INTEGER PRIMARY KEY AUTOINCREMENT, set_no TEXT, price REAL,
      cur TEXT, qty INTEGER, shop TEXT, date TEXT, sell_alerted INTEGER DEFAULT 0, sold INTEGER DEFAULT 0,
      sale REAL, sale_date TEXT)`),
    env.DB.prepare(`CREATE TABLE IF NOT EXISTS deals (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, set_no TEXT,
      name TEXT, price REAL, cur TEXT, discount INTEGER, score INTEGER, merchant TEXT, country TEXT, link TEXT, title TEXT)`),
    env.DB.prepare(`CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)`),
    env.DB.prepare(`CREATE TABLE IF NOT EXISTS valuations (set_no TEXT, day TEXT, value REAL, PRIMARY KEY (set_no, day))`),
  ]);
}

async function kvGet(env, k) {
  const r = await env.DB.prepare("SELECT v FROM kv WHERE k = ?").bind(k).first();
  return r ? r.v : null;
}
const kvSet = (env, k, v) =>
  env.DB.prepare("INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v").bind(k, String(v)).run();

// ---------------------------------------------------------------- noms des sets (watchlist publique)
let NAMES = null;
async function names() {
  if (NAMES) return NAMES;
  NAMES = {};
  try {
    const csv = await (await fetch(REPO_RAW)).text();
    for (const line of csv.split("\n").slice(1)) {
      const [set, nom] = line.split(",");
      if (set && nom) NAMES[set.trim()] = nom.trim();
    }
  } catch (e) {}
  return NAMES;
}

// ---------------------------------------------------------------- valeur de marché (Brickfact MCP)
async function brickfactValue(set) {
  const url = "https://mcp.brickfact.com/mcp";
  const base = { "Content-Type": "application/json", Accept: "application/json, text/event-stream", "User-Agent": "lego-bot-worker" };
  const post = async (body, sid) => {
    const r = await fetch(url, { method: "POST", headers: sid ? { ...base, "Mcp-Session-Id": sid } : base, body: JSON.stringify(body) });
    let raw = await r.text();
    for (const l of raw.split("\n")) if (l.startsWith("data:")) raw = l.slice(5).trim();
    return { sid: r.headers.get("Mcp-Session-Id") || sid, json: raw.trim() ? JSON.parse(raw) : null };
  };
  const init = await post({ jsonrpc: "2.0", id: 0, method: "initialize",
    params: { protocolVersion: "2025-03-26", capabilities: {}, clientInfo: { name: "lego-bot-worker", version: "1.0" } } });
  await post({ jsonrpc: "2.0", method: "notifications/initialized" }, init.sid);
  const res = await post({ jsonrpc: "2.0", id: 1, method: "tools/call",
    params: { name: "get_price_history", arguments: { market: "FR", set_id: parseInt(set), range: "1M" } } }, init.sid);
  const sc = res.json?.result?.structuredContent || {};
  const pts = (sc.points || []).filter((p) => p.value).sort((a, b) => (a.date < b.date ? -1 : 1));
  return pts.length ? pts[pts.length - 1].value : null;
}

async function marketValue(env, set) {
  const row = await env.DB.prepare("SELECT value FROM valuations WHERE set_no = ? AND day = ?").bind(set, today()).first();
  if (row) return row.value;
  let v = null;
  try { v = await brickfactValue(set); } catch (e) {}
  if (v) await env.DB.prepare("INSERT OR REPLACE INTO valuations VALUES (?, ?, ?)").bind(set, today(), v).run();
  return v;
}

let FX = undefined;
async function eurToGbp() {
  if (FX !== undefined) return FX;
  try { FX = (await (await fetch("https://api.frankfurter.app/latest?from=EUR&to=GBP")).json()).rates.GBP; }
  catch (e) { FX = null; }
  return FX;
}

async function valueIn(env, cur, set) {
  const mv = await marketValue(env, set);
  if (!mv) return null;
  if (cur === "EUR") return mv;
  const r = await eurToGbp();
  return r ? mv * r : null;
}

// ---------------------------------------------------------------- commandes
const PRICE_TOKEN = /^[£€]?\d+(?:[.,]\d{1,2})?[£€]?$/;

function parseAchat(args) {
  let num = null, price = null, cur = "EUR", qty = 1;
  const shop = [];
  for (const t of args) {
    if (num === null && /^\d{4,6}(-1)?$/.test(t)) num = t.split("-")[0];
    else if (price === null && PRICE_TOKEN.test(t)) { if (t.includes("£")) cur = "GBP"; price = toFloat(t.replace(/[£€]/g, "")); }
    else if (/^[xX]\d{1,3}$/.test(t)) qty = parseInt(t.slice(1));
    else if (["£", "gbp"].includes(t.toLowerCase())) cur = "GBP";
    else if (["€", "eur"].includes(t.toLowerCase())) cur = "EUR";
    else shop.push(t);
  }
  if (!num || price === null || isNaN(price)) return null;
  return { set: num, price, cur, qty, shop: shop.join(" ") };
}

async function exportStock(env) {
  const { results } = await env.DB.prepare("SELECT * FROM stock ORDER BY id").all();
  const conv = (r) => ({ id: r.id, set: r.set_no, price: r.price, cur: r.cur, qty: r.qty, shop: r.shop || "",
    date: r.date, sell_alerted: !!r.sell_alerted, ...(r.sold ? { sale: r.sale, sale_date: r.sale_date } : {}) });
  return { items: results.filter((r) => !r.sold).map(conv), sold: results.filter((r) => r.sold).map(conv) };
}

async function importStock(env, data) {
  const stmts = [env.DB.prepare("DELETE FROM stock")];
  for (const it of data.items || [])
    stmts.push(env.DB.prepare("INSERT INTO stock (set_no, price, cur, qty, shop, date, sell_alerted) VALUES (?,?,?,?,?,?,?)")
      .bind(it.set, it.price, it.cur, it.qty, it.shop || "", it.date || today(), it.sell_alerted ? 1 : 0));
  for (const it of data.sold || [])
    stmts.push(env.DB.prepare("INSERT INTO stock (set_no, price, cur, qty, shop, date, sold, sale, sale_date) VALUES (?,?,?,?,?,?,1,?,?)")
      .bind(it.set, it.price, it.cur, it.qty, it.shop || "", it.date || today(), it.sale, it.sale_date || it.date || today()));
  await env.DB.batch(stmts);
}

async function backup(env) {
  const data = JSON.stringify(await exportStock(env), null, 1);
  const form = new FormData();
  form.append("chat_id", env.CHAT_ID);
  form.append("disable_notification", "true");
  form.append("caption", "💾 Sauvegarde de ton stock (renvoie ce fichier au bot pour le restaurer)");
  form.append("document", new Blob([data], { type: "application/json" }), "stock.json");
  await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/sendDocument`, { method: "POST", body: form });
}

async function pinHelp(env) {
  const r = await send(env, HELP);
  if (r.ok) await tg(env, "pinChatMessage", { chat_id: env.CHAT_ID, message_id: r.result.message_id, disable_notification: true });
}

async function stockMessage(env) {
  const { items, sold } = await exportStock(env);
  if (!items.length) return [`📦 Ton stock est vide. Ajoute un achat avec /achat 75415 48.99`, null];
  const nm = await names();
  const lines = ["📦 <b>Ton stock</b>"], buttons = [], tot = {};
  for (const it of items) {
    const s = sym(it.cur), cost = it.price * it.qty, v = await valueIn(env, it.cur, it.set);
    let row = `\n<b>${it.set}</b> ${esc(nm[it.set] || "")} · x${it.qty} à ${s}${it.price.toFixed(2)}`;
    if (it.shop) row += ` (${esc(it.shop)})`;
    if (v) {
      const gain = (v - it.price) * it.qty;
      row += `\n   Valeur marché ~${s}${v.toFixed(0)}/boîte → <b>${gain >= 0 ? "+" : ""}${gain.toFixed(0)} ${s}</b> (${v >= it.price ? "+" : ""}${((v / it.price - 1) * 100).toFixed(0)} %)`;
      tot[it.cur] = tot[it.cur] || [0, 0];
      tot[it.cur][0] += cost; tot[it.cur][1] += v * it.qty;
    } else row += "\n   Valeur marché indisponible";
    lines.push(row);
    buttons.push([{ text: `✅ Vendu : ${it.set} (x${it.qty})`, callback_data: `sold:${it.id}` }]);
  }
  for (const [cur, [c, v]] of Object.entries(tot))
    lines.push(`\n💰 Total ${sym(cur)} : payé ${sym(cur)}${c.toFixed(0)}, vaut ~${sym(cur)}${v.toFixed(0)} (${v - c >= 0 ? "+" : ""}${(v - c).toFixed(0)})`);
  if (sold.length) {
    const real = {};
    for (const x of sold) real[x.cur] = (real[x.cur] || 0) + (x.sale - x.price) * x.qty;
    lines.push("🏁 Déjà réalisé : " + Object.entries(real).map(([c, v]) => `${v >= 0 ? "+" : ""}${v.toFixed(0)} ${sym(c)}`).join(", "));
  }
  lines.push("\n<i>Valeur marché : set scellé, Brickfact. Indicatif.</i>");
  return [lines.join("\n"), { inline_keyboard: buttons }];
}

async function dealsMessage(env, shop) {
  const since = new Date(Date.now() - 7 * 86400e3).toISOString();
  let { results } = await env.DB.prepare("SELECT * FROM deals WHERE ts >= ? ORDER BY id DESC").bind(since).all();
  if (shop) results = results.filter((d) => `${d.merchant} ${d.title}`.toLowerCase().includes(shop.toLowerCase()));
  if (!results.length) return `Aucun deal ${shop ? "chez " + esc(shop) + " " : ""}sur les 7 derniers jours qui passe tes règles.`;
  const best = {};
  for (const d of results) {
    const k = d.set_no + d.cur;
    if (!best[k] || d.price < best[k].price) best[k] = d;
  }
  const top = Object.values(best).sort((a, b) => b.score - a.score).slice(0, 10);
  const lines = [`🔥 <b>Meilleurs deals${shop ? " · " + esc(shop) : ""}</b> (7 derniers jours)`];
  for (const d of top) {
    const dot = d.score >= 75 ? "🟢" : d.score >= 62 ? "🟡" : "🟠";
    lines.push(`${dot} ${d.score} · <b>-${d.discount} %</b> · ${d.set_no} ${esc(d.name)}\n   ${sym(d.cur)}${d.price.toFixed(2)} · ` +
      `${esc(d.merchant || "?")} ${FLAGS[d.country] || ""} · <a href="${esc(d.link).replace(/"/g, "&quot;")}">voir</a> · ${d.ts.slice(8, 10)}/${d.ts.slice(5, 7)}`);
  }
  lines.push("<i>Le prix a pu changer depuis : vérifie avant d'acheter.</i>");
  return lines.join("\n");
}

async function handleUpdate(env, update) {
  const cb = update.callback_query;
  if (cb) {
    await tg(env, "answerCallbackQuery", { callback_query_id: cb.id });
    if ((cb.data || "").startsWith("sold:")) {
      const id = parseInt(cb.data.split(":")[1]);
      const it = await env.DB.prepare("SELECT * FROM stock WHERE id = ? AND sold = 0").bind(id).first();
      if (it) {
        await kvSet(env, "pending_sale", id);
        await send(env, `Prix de vente par boîte pour ${it.set_no} (x${it.qty}) ?\nRéponds juste le prix, par ex. 85 (devise : ${sym(it.cur)}).`,
          { reply_markup: { force_reply: true, input_field_placeholder: "Prix de vente" } });
      }
    }
    return;
  }
  const msg = update.message || {};
  // Restauration d'une sauvegarde
  if (msg.document && (msg.document.file_name || "").endsWith(".json")) {
    const f = await tg(env, "getFile", { file_id: msg.document.file_id });
    const data = await (await fetch(`https://api.telegram.org/file/bot${env.BOT_TOKEN}/${f.result.file_path}`)).json();
    if (!data || !Array.isArray(data.items)) return send(env, "⚠️ Ce fichier n'est pas une sauvegarde de stock.");
    await importStock(env, data);
    return send(env, `♻️ Stock restauré : ${data.items.length} set(s) en stock, ${(data.sold || []).length} vente(s).`);
  }
  const text = (msg.text || "").trim();
  if (!text) return;

  // Réponse au « prix de vente ? »
  const pending = await kvGet(env, "pending_sale");
  if (pending && PRICE_TOKEN.test(text)) {
    await kvSet(env, "pending_sale", "");
    const it = await env.DB.prepare("SELECT * FROM stock WHERE id = ? AND sold = 0").bind(parseInt(pending)).first();
    if (!it) return;
    const sale = toFloat(text.replace(/[£€]/g, ""));
    await env.DB.prepare("UPDATE stock SET sold = 1, sale = ?, sale_date = ? WHERE id = ?").bind(sale, today(), it.id).run();
    const gain = (sale - it.price) * it.qty;
    await send(env, `🏁 Vente enregistrée : ${it.set_no} x${it.qty} à ${sym(it.cur)}${sale.toFixed(2)}\n` +
      `Plus-value brute : <b>${gain >= 0 ? "+" : ""}${gain.toFixed(2)} ${sym(it.cur)}</b> (${sale >= it.price ? "+" : ""}${((sale / it.price - 1) * 100).toFixed(0)} %)`);
    return backup(env);
  }

  const parts = text.split(/\s+/);
  const cmd = parts[0].split("@")[0].toLowerCase();
  if (cmd === "/achat") {
    const r = parseAchat(parts.slice(1));
    if (!r) return send(env, "Format : /achat 75415 48.99 (options : x2, £, magasin)");
    await env.DB.prepare("INSERT INTO stock (set_no, price, cur, qty, shop, date) VALUES (?,?,?,?,?,?)")
      .bind(r.set, r.price, r.cur, r.qty, r.shop, today()).run();
    const nm = (await names())[r.set] || "";
    await send(env, `✅ Achat enregistré : ${r.set} ${esc(nm)} x${r.qty} à ${sym(r.cur)}${r.price.toFixed(2)}` +
      (r.shop ? ` (${esc(r.shop)})` : "") + `\nJe te préviens quand il prend +${SELL_TARGET} %. /stock pour voir le tout.`);
    return backup(env);
  }
  if (cmd === "/stock") {
    const [t, kb] = await stockMessage(env);
    return send(env, t, kb ? { reply_markup: kb } : {});
  }
  if (cmd === "/deals") return send(env, await dealsMessage(env, parts.slice(1).join(" ") || null));
  if (["/aide", "/help", "/start"].includes(cmd)) return pinHelp(env);
  return send(env, HELP);
}

// ---------------------------------------------------------------- appels du bot GitHub
async function daily(env) {
  if ((await kvGet(env, "last_daily")) === today()) return { skipped: true };
  await kvSet(env, "last_daily", today());
  const { results } = await env.DB.prepare("SELECT * FROM stock WHERE sold = 0 AND sell_alerted = 0").all();
  let n = 0;
  for (const it of results) {
    const v = await valueIn(env, it.cur, it.set_no);
    if (v && (v / it.price - 1) * 100 >= SELL_TARGET) {
      await env.DB.prepare("UPDATE stock SET sell_alerted = 1 WHERE id = ?").bind(it.id).run();
      await send(env, `📈 <b>Moment de vendre ?</b> ${it.set_no} vaut maintenant ~${sym(it.cur)}${v.toFixed(0)} ` +
        `(acheté ${sym(it.cur)}${it.price.toFixed(2)}, +${((v / it.price - 1) * 100).toFixed(0)} %).\nVérifie les ventes réelles avant de mettre en vente.`);
      n++;
    }
  }
  await env.DB.prepare("DELETE FROM deals WHERE ts < ?").bind(new Date(Date.now() - 8 * 86400e3).toISOString()).run();
  await env.DB.prepare("DELETE FROM valuations WHERE day < ?").bind(new Date(Date.now() - 3 * 86400e3).toISOString().slice(0, 10)).run();
  return { alerts: n };
}

async function setup(env, workerUrl) {
  await tg(env, "setWebhook", { url: workerUrl, secret_token: env.SHARED_SECRET, allowed_updates: ["message", "callback_query"] });
  await tg(env, "setMyCommands", { commands: [
    { command: "achat", description: "Enregistrer un achat : /achat 75415 48.99" },
    { command: "stock", description: "Mon stock, sa valeur, bouton Vendu" },
    { command: "deals", description: "Meilleurs deals (option : magasin)" },
    { command: "aide", description: "Liste des commandes (épinglée)" },
  ] });
  if (!(await kvGet(env, "help_pinned"))) { await pinHelp(env); await kvSet(env, "help_pinned", "1"); }
  return { ok: true };
}

// ---------------------------------------------------------------- point d'entrée
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const fromBot = request.headers.get("X-Shared-Secret") === env.SHARED_SECRET;
    const fromTelegram = request.headers.get("X-Telegram-Bot-Api-Secret-Token") === env.SHARED_SECRET;
    if (!fromBot && !fromTelegram) return new Response("forbidden", { status: 403 });
    await schema(env);

    if (fromBot) {
      if (url.pathname === "/health") return Response.json({ ok: true });
      if (url.pathname === "/setup") return Response.json(await setup(env, `${url.origin}/`));
      if (url.pathname === "/export") return Response.json(await exportStock(env));
      if (url.pathname === "/import" && request.method === "POST") { await importStock(env, await request.json()); return Response.json({ ok: true }); }
      if (url.pathname === "/daily" && request.method === "POST") return Response.json(await daily(env));
      if (url.pathname === "/deals" && request.method === "POST") {
        const list = await request.json();
        const stmts = list.map((d) => env.DB.prepare(
          "INSERT INTO deals (ts, set_no, name, price, cur, discount, score, merchant, country, link, title) VALUES (?,?,?,?,?,?,?,?,?,?,?)")
          .bind(d.ts, d.set, d.name, d.price, d.cur, d.discount, d.score, d.merchant || "", d.country || "", d.link || "", d.title || ""));
        if (stmts.length) await env.DB.batch(stmts);
        return Response.json({ stored: stmts.length });
      }
      return new Response("not found", { status: 404 });
    }

    // Message Telegram : seuls tes messages sont traités
    const update = await request.json();
    const chat = String(update.callback_query?.message?.chat?.id || update.message?.chat?.id || "");
    if (chat !== String(env.CHAT_ID)) return new Response("ignored");
    try {
      await handleUpdate(env, update);
    } catch (e) {
      await send(env, `⚠️ Erreur : ${esc(String(e)).slice(0, 150)}`);
    }
    return new Response("ok");
  },
};
