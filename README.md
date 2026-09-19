# Loop Co — Coffee Shop POS

A full-stack, server-rendered point-of-sale system for a coffee shop.
**Flask + SQLite + Jinja2**, localhost-only, zero cost, zero external services.

Brand: **Loop Co** — "The loop that never breaks." (forest green `#1B2E1B`, bark brown `#3D2B1F`, brass `#C2A05A`, cream `#F4F1EA`).

---

## Quick Start

```bash
# 1. Install dependencies (Python 3.12+)
pip install -r requirements.txt

# 2. Seed the database (shop settings, categories, 12-product menu) — idempotent
python seed.py

# 3. Run the server (binds to 127.0.0.1 only)
python app.py

# 4. Open the POS
#    http://127.0.0.1:5000
```

Health check: `curl http://127.0.0.1:5000/health` → `{"status":"ok","project":"CoffeeShopPOS","v":"V2"}`

## Features

| Area | What it does |
|---|---|
| Design system | Shared `static/css/app.css` (tokens, components, responsive) + `base.html` shell — sticky icon nav with active state, flash toasts, styled error pages, print CSS, reduced-motion support. Zero external assets. |
| POS terminal | Tap-to-add product tiles, category pills, live product search (`?q=`), sticky cart with qty steppers, discount, tender, payment method — server-side cart (survives refresh/browser restart) |
| Checkout | Real totals from cart contents, stock validation (blocks overselling), integer-cents math, transactional (BEGIN IMMEDIATE / COMMIT / ROLLBACK) |
| Order lifecycle | State machine `pending → completed → voided / refunded`; terminal states are final and enforced at route + DB level |
| Void | Restores full stock, records movement (`reason='void'`), stores void record + audit event |
| Refund | Capped at original order total, proportional stock reversal (`reason='refund'`), refund record + audit event |
| Dashboard | Today's sales / orders / average, refunds & voids, low-stock meters, recent orders with status badges |
| Reports | Sales by product with comparison bars, discounts, refund/void totals — reconciliation-ready |
| Receipt | Loop Co branded, status banners (VOIDED / REFUNDED / PENDING), frozen historical totals, print-friendly |
| Customization | Editable shop name, tax rate, receipt tagline/footer, order-number prefix — applied live everywhere. Currency is fixed to Philippine peso (₱) |
| Consumer menu | `/menu` — standalone customer-facing view of active products only: hero with shop identity, category sections, descriptions, House-favorite highlights, sold-out states, print-friendly. Zero ordering capability |
| Product & category management | Add, edit, reprice, restock, deactivate/delete products; add/delete categories with history protection |
| Order holds | `Hold order` parks the cart as a pending tab; resume it back into the cart or discard from Orders |
| Data reset | Danger zone: clear all sales history and restore seed stock levels |
| CSV export | `/export` downloads all orders |

## Architecture

Server-rendered monolith (per `LELOUCH-STRATEGIC-PLAN-AND-ARCHITECTURE.md`):

```
Browser (HTML forms, session cookie) 
  → Flask routes (app.py, 16 routes) 
    → helpers: get_or_create_cart / compute_cart_totals / state machine 
      → sqlite3 (parameterized SQL, PRAGMA foreign_keys, integer cents)
        → coffee.sqlite3 (13 tables, schema.sql, idempotent)
```

- **Money is integer cents everywhere.** The browser UI takes dollars and converts;
  the HTTP API accepts cents (`discount_cents`, `tendered_cents`, `amount`) — tests use the cents contract.
- **All money mutations are transactional** (BEGIN IMMEDIATE → ... → COMMIT, with ROLLBACK on any failure).
- **Cart lives server-side** (`carts` + `cart_items`), referenced by the session cookie; no JSON-in-URL.
- **Schema is idempotent** (`CREATE TABLE IF NOT EXISTS`) — safe re-initialization on every boot; `init_db()` also migrates pre-V2 databases (adds `orders.voided_at` / `orders.refunded_at` when missing).
- **"Today" on the dashboard and reports means the shop's local day.** Timestamps are stored in UTC (SQLite default) and converted with `date(..., 'localtime')` at query time.
- **Seed data lives in `seed.py`, not `schema.sql`** — so test databases start empty for fixtures.

## Routes

| Route | Method | Purpose |
|---|---|---|
| `/health` | GET | Health JSON |
| `/` | GET | Dashboard |
| `/pos` | GET | POS terminal (`?category=N` filters the menu) |
| `/add-item` | POST | Append product to cart (merge on repeat) |
| `/update-cart` | POST | Set line qty (0 removes) |
| `/remove-item` | POST | Remove a cart line |
| `/checkout` | GET→redirect, POST | Complete the order |
| `/receipt` | GET | Receipt (`?order_id=N`, defaults to latest) |
| `/void-order` | POST | Void pending/completed order, restore stock |
| `/refund-order` | POST | Refund completed order (≤ total), restore stock |
| `/orders` | GET | Order history + void/refund actions |
| `/products` | GET | Catalog with stock levels |
| `/reports` | GET | Daily analytics |
| `/settings` | GET, POST | View + save shop settings; `/settings/reset` POST clears sales history |
| `/export` | GET | Orders CSV download |
| `/products/add`, `/products/edit`, `/products/delete` | POST | Product management |
| `/categories/add`, `/categories/delete` | POST | Category management |
| `/hold-order` | POST | Park the active cart as a pending order |
| `/resume-hold`, `/discard-hold` | POST | Resume a hold into the cart / delete it |

## Tests

```bash
python -m pytest tests/test_pos.py -v      # 17 functional tests
python tests/verify_acceptance.py          # acceptance suite: F1–F14, T1–T5
```

Test databases are created fresh in temp dirs; tests insert their own fixtures and never touch `coffee.sqlite3`.

## Documentation

- `schema.sql` — 13-table schema with CHECK-constrained state machines (order + cart lifecycle enforced at the DB level)
- `tests/` — functional pytest suite and the acceptance verification suite (state machine, inventory reversal, transactional integrity)
- `seed.py` — idempotent seeder: shop settings, categories, starter menu with descriptions and featured flags

### Security note

`app.py` signs session cookies with a built-in default key that is fine for single-user, localhost use — the intended mode for this app. If you ever expose it beyond your machine, set the `COFFEE_POS_SECRET` environment variable so sessions are signed with your own secret.

## Deploy a public demo (free, Render)

The repo ships with a `render.yaml` blueprint, so Render deploys it with a few clicks:

1. Push this repo to GitHub (it already is: `espherxyz/loop-co-pos`)
2. Create a free account at [render.com](https://render.com) **using your GitHub login**
3. Dashboard → **New + → Blueprint** → select `loop-co-pos`
4. When prompted for `POS_PASSWORD`, type the staff password you want — this protects the POS, orders, products, reports and settings behind a login
5. Deploy. Your live demo appears at `https://loop-co-pos-xxxx.onrender.com`

Behavior in the cloud: the customer menu (`/menu`) is public, everything else asks for the staff password; a fresh database auto-seeds with the starter menu (`AUTO_SEED=1`). The free tier sleeps after 15 minutes idle (first request then takes ~40s to wake) and the demo database resets on restarts — it's a demo, your real till stays local.

## License

[MIT](LICENSE) — use it, fork it, sell coffee with it.

## Out of Scope (by design)

No AI features, no external APIs, no SPA framework, no cloud services, no user accounts.
Local single-shop operation only.
