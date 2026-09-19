from pathlib import Path
import os
import uuid
import sqlite3, json, csv, io, datetime
from flask import Flask, g, render_template, request, redirect, url_for, session, Response, flash

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "coffee.sqlite3"
SCHEMA = ROOT / "schema.sql"

def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(DATABASE=str(DB_PATH))
    # Signs the session cookie. The built-in default is for single-user local
    # use; set COFFEE_POS_SECRET when running anywhere shared or exposed.
    app.secret_key = os.environ.get("COFFEE_POS_SECRET", "coffee-pos-v2-secret-key")
    if test_config:
        app.config.update(test_config)

    def get_db():
        if "db" not in g:
            g.db = sqlite3.connect(app.config["DATABASE"])
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys=ON")
        return g.db

    def init_db():
        db = get_db()
        db.executescript(SCHEMA.read_text(encoding="utf-8"))
        # Migrate DBs created before the V2 schema: CREATE TABLE IF NOT EXISTS
        # cannot add columns to an existing table.
        cols = {r[1] for r in db.execute("PRAGMA table_info(orders)").fetchall()}
        if "voided_at" not in cols:
            db.execute("ALTER TABLE orders ADD COLUMN voided_at TEXT DEFAULT NULL")
        if "refunded_at" not in cols:
            db.execute("ALTER TABLE orders ADD COLUMN refunded_at TEXT DEFAULT NULL")
        pcol = {r[1] for r in db.execute("PRAGMA table_info(products)").fetchall()}
        if "description" not in pcol:
            db.execute("ALTER TABLE products ADD COLUMN description TEXT DEFAULT ''")
        if "featured" not in pcol:
            db.execute("ALTER TABLE products ADD COLUMN featured INTEGER DEFAULT 0")
        scol = {r[1] for r in db.execute("PRAGMA table_info(settings)").fetchall()}
        for col, ddl in (
            ("receipt_tagline", "TEXT DEFAULT 'The loop that never breaks.'"),
            ("receipt_footer", "TEXT DEFAULT 'Thank you — come again.'"),
            ("order_prefix", "TEXT DEFAULT 'R-'"),
        ):
            if col not in scol:
                db.execute("ALTER TABLE settings ADD COLUMN " + col + " " + ddl)
        db.commit()

    app.get_db = get_db
    app.init_db = init_db

    def error(message, status=400, heading=None):
        # Styled error page; keeps the API status code and message text intact
        return render_template("error.html", message=message, status=status, heading=heading), status

    def get_cfg():
        # Shop settings for templates; falls back to brand defaults when unset
        if "cfg" not in g:
            try:
                row = get_db().execute("SELECT * FROM settings WHERE id=1").fetchone()
            except sqlite3.Error:
                row = None
            if row is None:
                row = {
                    "shop_name": "Loop Co", "currency_symbol": "₱", "tax_rate": 0.08,
                    "receipt_tagline": "The loop that never breaks.",
                    "receipt_footer": "Thank you — come again.", "order_prefix": "R-",
                }
            g.cfg = row
        return g.cfg

    @app.context_processor
    def inject_cfg():
        cfg = get_cfg()
        def money(cents):
            sym = cfg["currency_symbol"] if "currency_symbol" in cfg.keys() else "$"
            return "%s%.2f" % (sym or "$", (cents or 0) / 100.0)
        return {"cfg": cfg, "money": money}

    def next_order_number(db, prefix):
        base = prefix + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        num = base
        while db.execute("SELECT 1 FROM orders WHERE order_number=?", (num,)).fetchone():
            num = prefix + datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + str(uuid.uuid4())[:4]
        return num

    @app.teardown_appcontext
    def close_db(_e):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    # --- Helper: get or create active cart for session ---
    def get_or_create_cart(db):
        cid = session.get("cart_id")
        if cid:
            cart = db.execute("SELECT * FROM carts WHERE id=? AND status='active'", (cid,)).fetchone()
            if cart:
                return cart
        # Create new cart - UUID for guaranteed uniqueness
        sid = str(uuid.uuid4())
        db.execute("INSERT INTO carts (session_id) VALUES (?)", (sid,))
        cart_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        session["cart_id"] = str(cart_id)
        session.modified = True
        return db.execute("SELECT * FROM carts WHERE id=?", (cart_id,)).fetchone()

    def get_active_cart(db):
        cart_id = session.get("cart_id")
        if not cart_id:
            return None
        # Only status='active' carts are usable; a converted cart must never be
        # checked out again (back-button resubmit would double-charge).
        return db.execute(
            "SELECT * FROM carts WHERE id=? AND status='active'", (cart_id,)
        ).fetchone()

    # --- Helper: compute cart totals ---
    def compute_cart_totals(db, cart_id):
        items = db.execute(
            "SELECT ci.id as cart_item_id, ci.product_id, ci.qty, p.name, p.price, p.stock_qty, p.low_stock_threshold, p.active "
            "FROM cart_items ci JOIN products p ON ci.product_id=p.id WHERE ci.cart_id=?",
            (cart_id,)
        ).fetchall()
        subtotal = sum(i["qty"] * i["price"] for i in items)
        tax_rate = db.execute("SELECT tax_rate FROM settings WHERE id=1").fetchone()
        tax_rate_val = tax_rate["tax_rate"] if tax_rate else 0.08
        tax = int(round(subtotal * tax_rate_val))
        return items, subtotal, tax

    # ============ ROUTES ============

    @app.get("/health")
    def health():
        return {"status": "ok", "project": "CoffeeShopPOS", "v": "V2"}

    @app.get("/")
    def dashboard():
        db = get_db()
        today = datetime.date.today().isoformat()
        sales_today = db.execute(
            "SELECT COALESCE(SUM(total),0) FROM orders WHERE date(timestamp,'localtime')=? AND status='completed'",
            (today,)
        ).fetchone()[0] or 0
        orders_today = db.execute(
            "SELECT COUNT(*) FROM orders WHERE date(timestamp,'localtime')=? AND status='completed'",
            (today,)
        ).fetchone()[0] or 0
        avg_order = db.execute(
            "SELECT COALESCE(AVG(total),0) FROM orders WHERE date(timestamp,'localtime')=? AND status='completed'",
            (today,)
        ).fetchone()[0] or 0
        low = db.execute(
            "SELECT id,name,stock_qty FROM products WHERE stock_tracked=1 AND stock_qty <= low_stock_threshold AND active=1 ORDER BY stock_qty ASC LIMIT 5"
        ).fetchall()
        recent = db.execute(
            "SELECT id,order_number,status,total,timestamp FROM orders ORDER BY id DESC LIMIT 5"
        ).fetchall()
        voids_today = db.execute(
            "SELECT COUNT(*) FROM orders WHERE date(timestamp,'localtime')=? AND status='voided'",
            (today,)
        ).fetchone()[0] or 0
        refunds_today = db.execute(
            "SELECT COUNT(*) FROM orders WHERE date(timestamp,'localtime')=? AND status='refunded'",
            (today,)
        ).fetchone()[0] or 0
        refund_total = db.execute(
            "SELECT COALESCE(SUM(r.amount),0) FROM refunds r JOIN orders o ON r.order_id=o.id WHERE date(o.timestamp,'localtime')=?",
            (today,)
        ).fetchone()[0] or 0
        void_total = db.execute(
            "SELECT COALESCE(SUM(v.amount),0) FROM voids v JOIN orders o ON v.order_id=o.id WHERE date(o.timestamp,'localtime')=?",
            (today,)
        ).fetchone()[0] or 0
        refund_count = db.execute(
            "SELECT COUNT(*) FROM refunds r JOIN orders o ON r.order_id=o.id WHERE date(o.timestamp,'localtime')=?",
            (today,)
        ).fetchone()[0] or 0
        return render_template("dashboard.html",
            sales_today=sales_today, orders_today=orders_today, avg_order=int(avg_order or 0),
            low_stock=low, recent=recent, voids_today=voids_today, refunds_today=refunds_today,
            refund_total=refund_total, void_total=void_total, refund_count=refund_count)

    @app.get("/pos")
    def pos():
        db = get_db()
        cart = get_active_cart(db)
        if not cart:
            cart = get_or_create_cart(db)

        # Handle ?order_id=N pre-population (F2)
        order_id_param = request.args.get("order_id", type=int)
        if order_id_param:
            order = db.execute("SELECT * FROM orders WHERE id=?", (order_id_param,)).fetchone()
            if order and order["status"] in ("completed", "pending"):
                # Clear current cart items and repopulate from order
                db.execute("DELETE FROM cart_items WHERE cart_id=?", (cart["id"],))
                order_items = db.execute(
                    "SELECT product_id, qty, unit_price, modifier_name, modifier_price_delta FROM order_items WHERE order_id=?",
                    (order_id_param,)
                ).fetchall()
                for oi in order_items:
                    db.execute(
                        "INSERT INTO cart_items(cart_id, product_id, qty) VALUES (?,?,?)",
                        (cart["id"], oi["product_id"], oi["qty"])
                    )
                db.execute("UPDATE carts SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (cart["id"],))
                session.modified = True

        cart_items, subtotal, tax = compute_cart_totals(db, cart["id"])
        total = subtotal + tax  # cart display doesn't include discount input yet

        # Check if any cart product is inactive
        has_inactive = any(i["active"] == 0 for i in cart_items)

        cats = db.execute("SELECT * FROM categories ORDER BY sort_order").fetchall()
        category_id = request.args.get("category", type=int)
        q = (request.args.get("q") or "").strip()
        where, params = ["p.active=1"], []
        if category_id:
            where.append("p.category_id=?")
            params.append(category_id)
        if q:
            where.append("p.name LIKE ?")
            params.append("%" + q + "%")
        prods = db.execute(
            "SELECT p.*, c.name as cat FROM products p JOIN categories c ON p.category_id=c.id "
            "WHERE " + " AND ".join(where) + " ORDER BY c.sort_order, p.name",
            params
        ).fetchall()
        tax_rate_row = db.execute("SELECT tax_rate FROM settings WHERE id=1").fetchone()
        tax_rate_val = tax_rate_row["tax_rate"] if tax_rate_row else 0.08

        return render_template("pos.html",
            cats=cats, prods=prods, cart_items=cart_items, subtotal=subtotal, tax=tax, total=total,
            cart_empty=len(cart_items) == 0, has_inactive=has_inactive, tax_rate=tax_rate_val,
            selected_category=category_id, q=q)

    @app.post("/add-item")
    def add_item():
        db = get_db()
        try:
            pid = int(request.form.get("product_id", 0))
        except (ValueError, TypeError):
            return error("product_id required", 400)
        try:
            qty = int(request.form.get("qty", 1))
        except (ValueError, TypeError):
            return error("Invalid quantity", 400)
        if pid <= 0:
            return error("product_id required", 400)
        prod = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not prod or prod["active"] == 0:
            return error("Invalid or inactive product", 400)
        if qty < 1 or qty > 99:
            return error("Invalid quantity", 400)
        cart = get_or_create_cart(db)

        # B2 fix: Append semantics - check if product already in cart
        existing = db.execute(
            "SELECT * FROM cart_items WHERE cart_id=? AND product_id=?",
            (cart["id"], pid)
        ).fetchone()
        if existing:
            new_qty = existing["qty"] + qty
            if new_qty > 99:
                return error("Invalid quantity", 400)
            db.execute("UPDATE cart_items SET qty=? WHERE id=?", (new_qty, existing["id"]))
        else:
            db.execute(
                "INSERT INTO cart_items(cart_id, product_id, qty) VALUES (?,?,?)",
                (cart["id"], pid, qty)
            )

        db.execute("UPDATE carts SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (cart["id"],))
        db.commit()
        return redirect(url_for("pos"))

    @app.post("/remove-item")
    def remove_item():
        db = get_db()
        try:
            cart_item_id = int(request.form.get("cart_item_id", 0))
        except (ValueError, TypeError):
            return error("cart_item_id required", 400)
        cart = get_active_cart(db)
        if not cart:
            return error("Cart item not found", 404)
        item = db.execute(
            "SELECT * FROM cart_items WHERE id=? AND cart_id=?",
            (cart_item_id, cart["id"])
        ).fetchone()
        if not item:
            return error("Cart item not found", 404)
        db.execute("DELETE FROM cart_items WHERE id=?", (cart_item_id,))
        db.execute("UPDATE carts SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (cart["id"],))
        db.commit()
        return redirect(url_for("pos"))

    @app.post("/update-cart")
    def update_cart():
        db = get_db()
        try:
            cart_item_id = int(request.form.get("cart_item_id", 0))
            qty = int(request.form.get("qty", 0))
        except (ValueError, TypeError):
            return error("Invalid input", 400)
        if qty < 0 or qty > 99:
            return error("Invalid quantity", 400)
        cart = get_active_cart(db)
        if not cart:
            return error("Cart item not found", 404)
        item = db.execute(
            "SELECT * FROM cart_items WHERE id=? AND cart_id=?",
            (cart_item_id, cart["id"])
        ).fetchone()
        if not item:
            return error("Cart item not found", 404)
        if qty == 0:
            # Treat as remove
            db.execute("DELETE FROM cart_items WHERE id=?", (cart_item_id,))
        else:
            db.execute("UPDATE cart_items SET qty=? WHERE id=?", (qty, cart_item_id))

        db.execute("UPDATE carts SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (cart["id"],))
        db.commit()
        return redirect(url_for("pos"))

    @app.get("/checkout")
    def checkout_get():
        return redirect(url_for("pos"))

    @app.post("/checkout")
    def checkout_post():
        db = get_db()
        cart = get_active_cart(db)
        if not cart:
            return error("Cart is empty", 400)
        cart_items = db.execute(
            "SELECT ci.id as cart_item_id, ci.product_id, ci.qty, p.name, p.price, p.stock_qty "
            "FROM cart_items ci JOIN products p ON ci.product_id=p.id WHERE ci.cart_id=?",
            (cart["id"],)
        ).fetchall()
        if not cart_items:
            return error("Cart is empty", 400)
        # Money input: form may post cents (API contract) or dollars (browser UI)
        raw_discount = request.form.get("discount_cents", "").strip()
        raw_discount_dollars = request.form.get("discount_dollars", "").strip()
        try:
            if raw_discount:
                discount_cents = int(raw_discount)
            elif raw_discount_dollars:
                discount_cents = int(round(float(raw_discount_dollars) * 100))
            else:
                discount_cents = 0
        except (ValueError, TypeError):
            return error("Invalid discount", 400)
        if discount_cents < 0:
            return error("Discount cannot be negative", 400)
        raw_tendered = request.form.get("tendered_cents", "").strip()
        raw_tendered_dollars = request.form.get("tendered_dollars", "").strip()
        try:
            if raw_tendered:
                tendered_cents = int(raw_tendered)
            elif raw_tendered_dollars:
                tendered_cents = int(round(float(raw_tendered_dollars) * 100))
            else:
                return error("Tendered must be a number", 400)
        except (ValueError, TypeError):
            return error("Tendered must be a number", 400)
        if tendered_cents <= 0:
            return error("Tendered must be > 0", 400)
        payment_method = request.form.get("payment_method", "cash") or "cash"

        try:
            db.execute("BEGIN IMMEDIATE")

            # Step 2: Validate stock (B7)
            insufficient = []
            for item in cart_items:
                prod = db.execute(
                    "SELECT * FROM products WHERE id=?",
                    (item["product_id"],)
                ).fetchone()
                if item["qty"] > prod["stock_qty"]:
                    insufficient.append(
                        f"{prod['name']} has {prod['stock_qty']} available but {item['qty']} requested"
                    )
            if insufficient:
                db.execute("ROLLBACK")
                return error("Insufficient stock: " + "; ".join(insufficient), 400)
            # Step 3: Compute totals (B1)
            subtotal = sum(item["qty"] * item["price"] for item in cart_items)
            tax_rate_row = db.execute("SELECT tax_rate FROM settings WHERE id=1").fetchone()
            tax_rate_val = tax_rate_row["tax_rate"] if tax_rate_row else 0.08
            tax = int(round(subtotal * tax_rate_val))
            discount = discount_cents
            if discount > subtotal:
                db.execute("ROLLBACK")
                return error("Discount exceeds subtotal", 400)
            total = subtotal - discount + tax

            # Step 4: Validate payment
            if tendered_cents < total:
                db.execute("ROLLBACK")
                return error("Insufficient tendered: need %d, got %d" % (total, tendered_cents), 400)
            change = tendered_cents - total

            # Step 5: Insert order (second-resolution number; suffix on collision
            # so two checkouts in the same second cannot violate the UNIQUE index)
            pref_row = db.execute("SELECT order_prefix FROM settings WHERE id=1").fetchone()
            prefix = pref_row["order_prefix"] if pref_row and pref_row["order_prefix"] else "R-"
            order_number = next_order_number(db, prefix)
            db.execute(
                "INSERT INTO orders(order_number,status,subtotal,discount_amount,tax_amount,total,tendered,change) VALUES (?,?,?,?,?,?,?,?)",
                (order_number, "completed", subtotal, discount, tax, total, tendered_cents, change)
            )
            order_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

            # Step 6: Insert order_items
            for item in cart_items:
                db.execute(
                    "INSERT INTO order_items(order_id,product_id,qty,unit_price,modifier_name,modifier_price_delta) VALUES (?,?,?,?,?,?)",
                    (order_id, item["product_id"], item["qty"], item["price"], "", 0)
                )

            # Step 7: Decrement inventory + movements
            for item in cart_items:
                db.execute(
                    "UPDATE products SET stock_qty = stock_qty - ? WHERE id=?",
                    (item["qty"], item["product_id"])
                )
                db.execute(
                    "INSERT INTO inventory_movements(product_id,order_id,delta,reason) VALUES (?,?,?,?)",
                    (item["product_id"], order_id, -item["qty"], "sale")
                )

            # Step 8: Insert payment
            db.execute(
                "INSERT INTO payments(order_id,method,tendered,change) VALUES (?,?,?,?)",
                (order_id, payment_method, tendered_cents, change)
            )

            # Step 9: Insert audit event (B8 fix: json.dumps)
            details = json.dumps({
                "total": total, "subtotal": subtotal, "tax": tax, "discount": discount,
                "tendered": tendered_cents, "change": change, "payment_method": payment_method
            })
            db.execute(
                "INSERT INTO audit_events(order_id,event_type,details_json) VALUES (?,?,?)",
                (order_id, "completed", details)
            )

            # Step 10: Mark cart
            db.execute(
                "UPDATE carts SET status='converted', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (cart["id"],)
            )

            # Step 11: Commit
            db.commit()
            flash("Order %s completed — change $%.2f" % (order_number, change / 100), "success")
            return redirect(url_for("receipt", order_id=order_id))

        except Exception as e:
            db.execute("ROLLBACK")
            return error("Checkout failed: %s" % str(e), 400)
    @app.get("/receipt")
    def receipt():
        db = get_db()
        order_id = request.args.get("order_id", type=int)
        if order_id is None:
            # B9 fix: default to most recent order
            row = db.execute("SELECT id FROM orders ORDER BY id DESC LIMIT 1").fetchone()
            if row:
                order_id = row["id"]
            else:
                return error("Order not found", 404)
        order = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            return error("Order not found", 404)
        items = db.execute(
            "SELECT oi.*, p.name FROM order_items oi JOIN products p ON oi.product_id=p.id WHERE oi.order_id=?",
            (order_id,)
        ).fetchall()
        payment = db.execute("SELECT * FROM payments WHERE order_id=?", (order_id,)).fetchone()
        void_info = db.execute("SELECT * FROM voids WHERE order_id=?", (order_id,)).fetchone()
        refund_info = db.execute("SELECT * FROM refunds WHERE order_id=?", (order_id,)).fetchone()

        status = order["status"]
        status_labels = {"pending": "PENDING", "completed": "COMPLETED", "voided": "VOIDED", "refunded": "REFUNDED"}
        status_classes = {"pending": "status-pending", "completed": "status-completed", "voided": "status-voided", "refunded": "status-refunded"}
        shop = db.execute("SELECT * FROM settings WHERE id=1").fetchone()

        return render_template("receipt.html",
            order=order, items=items, payment=payment, void_info=void_info, refund_info=refund_info,
            status_label=status_labels.get(status, status), status_class=status_classes.get(status, ""),
            shop=shop)

    @app.post("/void-order")
    def void_order():
        db = get_db()
        try:
            order_id = int(request.form.get("order_id", 0))
        except (ValueError, TypeError):
            return error("order_id required", 400)
        order = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            return error("Order not found", 404)
        if order["status"] == "voided":
            return error("Order already voided", 400)
        if order["status"] == "refunded":
            return error("Cannot void refunded order", 400)
        if order["status"] not in ("pending", "completed"):
            return error("Cannot void order in status %s" % order["status"], 400)
        try:
            db.execute("BEGIN IMMEDIATE")

            reason = (request.form.get("reason") or "no reason given")[:500]
            amount = order["total"]

            # If completed, reverse inventory
            if order["status"] == "completed":
                order_items = db.execute(
                    "SELECT * FROM order_items WHERE order_id=?", (order_id,)
                ).fetchall()
                for oi in order_items:
                    db.execute(
                        "UPDATE products SET stock_qty = stock_qty + ? WHERE id=?",
                        (oi["qty"], oi["product_id"])
                    )
                    db.execute(
                        "INSERT INTO inventory_movements(product_id,order_id,delta,reason) VALUES (?,?,?,?)",
                        (oi["product_id"], order_id, oi["qty"], "void")
                    )

            # Insert void record
            db.execute(
                "INSERT INTO voids(order_id,reason,amount) VALUES (?,?,?)",
                (order_id, reason, amount)
            )

            # Update order status
            db.execute(
                "UPDATE orders SET status='voided', voided_at=CURRENT_TIMESTAMP WHERE id=?",
                (order_id,)
            )

            # Insert audit event
            details = json.dumps({"order_total": amount, "reason": reason})
            db.execute(
                "INSERT INTO audit_events(order_id,event_type,details_json) VALUES (?,?,?)",
                (order_id, "voided", details)
            )

            db.commit()
            flash("Order #%s voided — stock restored" % order_id, "success")
            return redirect(url_for("orders"))

        except Exception as e:
            db.execute("ROLLBACK")
            return error("Void failed: %s" % str(e), 400)
    @app.post("/refund-order")
    def refund_order():
        db = get_db()
        try:
            order_id = int(request.form.get("order_id", 0))
        except (ValueError, TypeError):
            return error("order_id required", 400)
        order = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            return error("Order not found", 404)
        if order["status"] != "completed":
            return error("Cannot refund order in status %s" % order["status"], 400)
        # Money input: form may post cents (API contract) or dollars (browser UI)
        raw_amount = request.form.get("amount", "").strip()
        raw_amount_dollars = request.form.get("amount_dollars", "").strip()
        try:
            if raw_amount:
                amount = int(raw_amount)
            elif raw_amount_dollars:
                amount = int(round(float(raw_amount_dollars) * 100))
            else:
                return error("amount required", 400)
        except (ValueError, TypeError):
            return error("amount required", 400)
        if amount <= 0:
            return error("Refund amount must be > 0", 400)
        if amount > order["total"]:
            return error("Refund amount %d exceeds order total %d" % (amount, order["total"]), 400)
        reason = (request.form.get("reason") or "no reason given")[:500]

        try:
            db.execute("BEGIN IMMEDIATE")

            # Check for existing refund
            existing = db.execute("SELECT * FROM refunds WHERE order_id=?", (order_id,)).fetchone()
            if existing:
                db.execute("ROLLBACK")
                return error("Order already refunded", 400)
            # Calculate proportional refund quantities
            total_ratio = amount / order["total"]
            order_items = db.execute(
                "SELECT * FROM order_items WHERE order_id=?", (order_id,)
            ).fetchall()

            refund_allocations = []
            allocated = 0
            for i, oi in enumerate(order_items):
                refund_qty = int(round(oi["qty"] * total_ratio))
                if refund_qty > oi["qty"]:
                    refund_qty = oi["qty"]
                refund_allocations.append((oi["product_id"], refund_qty))
                allocated += refund_qty

            # If all rounded to 0 but ratio > 0, allocate 1 unit to highest-value item
            if allocated == 0 and total_ratio > 0 and order_items:
                highest = max(order_items, key=lambda x: x["unit_price"])
                refund_allocations = [(highest["product_id"], 1)]

            # Reverse inventory
            for pid, rqty in refund_allocations:
                db.execute(
                    "UPDATE products SET stock_qty = stock_qty + ? WHERE id=?",
                    (rqty, pid)
                )
                db.execute(
                    "INSERT INTO inventory_movements(product_id,order_id,delta,reason) VALUES (?,?,?,?)",
                    (pid, order_id, rqty, "refund")
                )

            # Insert refund record
            db.execute(
                "INSERT INTO refunds(order_id,amount,reason) VALUES (?,?,?)",
                (order_id, amount, reason)
            )

            # Update order status
            db.execute(
                "UPDATE orders SET status='refunded', refunded_at=CURRENT_TIMESTAMP WHERE id=?",
                (order_id,)
            )

            # Insert audit event
            details = json.dumps({
                "refund_amount": amount, "original_total": order["total"], "reason": reason
            })
            db.execute(
                "INSERT INTO audit_events(order_id,event_type,details_json) VALUES (?,?,?)",
                (order_id, "refunded", details)
            )

            db.commit()
            flash("Refund of $%.2f processed" % (amount / 100), "success")
            return redirect(url_for("orders"))

        except Exception as e:
            db.execute("ROLLBACK")
            return error("Refund failed: %s" % str(e), 400)
    @app.get("/orders")
    def orders():
        db = get_db()
        status = request.args.get("status")
        allowed = ("pending", "completed", "voided", "refunded")
        if status in allowed:
            rows = db.execute("SELECT * FROM orders WHERE status=? ORDER BY id DESC LIMIT 50", (status,)).fetchall()
        else:
            status = None
            rows = db.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 50").fetchall()
        counts = {r["status"]: r["n"] for r in db.execute(
            "SELECT status, COUNT(*) as n FROM orders GROUP BY status").fetchall()}
        return render_template("orders.html", rows=rows, status_filter=status, counts=counts)

    @app.get("/products")
    def products():
        db = get_db()
        rows = db.execute(
            "SELECT p.*, c.name as cat FROM products p JOIN categories c ON p.category_id=c.id ORDER BY c.sort_order, p.name"
        ).fetchall()
        cats = db.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM products p WHERE p.category_id=c.id) as n FROM categories c ORDER BY c.sort_order"
        ).fetchall()
        return render_template("products.html", rows=rows, cats=cats)

    @app.get("/reports")
    def reports():
        db = get_db()
        today = datetime.date.today().isoformat()
        sales = db.execute(
            "SELECT COALESCE(SUM(total),0) FROM orders WHERE date(timestamp,'localtime')=? AND status='completed'",
            (today,)
        ).fetchone()[0] or 0
        orders_cnt = db.execute(
            "SELECT COUNT(*) FROM orders WHERE date(timestamp,'localtime')=? AND status='completed'",
            (today,)
        ).fetchone()[0] or 0
        avg = db.execute(
            "SELECT COALESCE(AVG(total),0) FROM orders WHERE date(timestamp,'localtime')=? AND status='completed'",
            (today,)
        ).fetchone()[0] or 0
        by_prod = db.execute(
            "SELECT p.name, SUM(oi.qty) as qty, SUM(oi.qty*oi.unit_price) as rev FROM order_items oi JOIN products p ON oi.product_id=p.id JOIN orders o ON oi.order_id=o.id WHERE date(o.timestamp,'localtime')=? AND o.status='completed' GROUP BY p.id",
            (today,)
        ).fetchall()
        discount_total = db.execute(
            "SELECT COALESCE(SUM(discount_amount),0) FROM orders WHERE date(timestamp,'localtime')=? AND status='completed'",
            (today,)
        ).fetchone()[0] or 0
        refund_total = db.execute(
            "SELECT COALESCE(SUM(r.amount),0) FROM refunds r JOIN orders o ON r.order_id=o.id WHERE date(o.timestamp,'localtime')=?",
            (today,)
        ).fetchone()[0] or 0
        void_count = db.execute(
            "SELECT COUNT(*) FROM orders WHERE date(timestamp,'localtime')=? AND status='voided'",
            (today,)
        ).fetchone()[0] or 0
        void_total = db.execute(
            "SELECT COALESCE(SUM(v.amount),0) FROM voids v JOIN orders o ON v.order_id=o.id WHERE date(o.timestamp,'localtime')=?",
            (today,)
        ).fetchone()[0] or 0
        refund_count = db.execute(
            "SELECT COUNT(*) FROM refunds r JOIN orders o ON r.order_id=o.id WHERE date(o.timestamp,'localtime')=?",
            (today,)
        ).fetchone()[0] or 0

        return render_template("reports.html",
            sales=sales, orders_cnt=orders_cnt, avg=int(avg or 0), by_prod=by_prod,
            discount_total=discount_total, refund_total=refund_total, refund_count=refund_count,
            void_count=void_count, void_total=void_total)

    @app.get("/settings")
    def settings():
        return render_template("settings.html")

    @app.post("/settings")
    def settings_save():
        db = get_db()
        shop_name = (request.form.get("shop_name") or "").strip()
        tagline = (request.form.get("receipt_tagline") or "").strip()[:120]
        footer = (request.form.get("receipt_footer") or "").strip()[:160]
        prefix = (request.form.get("order_prefix") or "R-").strip()[:8]
        try:
            percent = float(request.form.get("tax_rate_percent", "8"))
        except (ValueError, TypeError):
            flash("Tax rate must be a number", "error")
            return redirect(url_for("settings"))
        if not shop_name:
            flash("Shop name cannot be empty", "error")
            return redirect(url_for("settings"))
        if not (0 <= percent <= 100):
            flash("Tax rate must be between 0 and 100 percent", "error")
            return redirect(url_for("settings"))
        db.execute("INSERT OR IGNORE INTO settings (id) VALUES (1)")
        db.execute(
            "UPDATE settings SET shop_name=?, tax_rate=?, receipt_tagline=?, receipt_footer=?, order_prefix=? WHERE id=1",
            (shop_name, percent / 100.0, tagline, footer, prefix),
        )
        db.commit()
        flash("Settings saved", "success")
        return redirect(url_for("settings"))

    @app.post("/settings/reset")
    def settings_reset():
        db = get_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            for t in ("cart_items", "carts", "audit_events", "inventory_movements",
                      "payments", "order_items", "voids", "refunds", "orders"):
                db.execute("DELETE FROM " + t)
            from seed import PRODUCTS
            for pid, _cat, _name, _price, stock, _low, _desc, _feat in PRODUCTS:
                db.execute("UPDATE products SET stock_qty=? WHERE id=?", (stock, pid))
            db.commit()
            flash("All sales history cleared and stock reset to seed levels", "success")
            return redirect(url_for("settings"))
        except Exception as e:
            db.execute("ROLLBACK")
            return error("Reset failed: %s" % str(e), 400)

    @app.get("/export")
    def export_csv():
        db = get_db()
        rows = db.execute(
            "SELECT id,order_number,status,subtotal,discount_amount,tax_amount,total,tendered,change,timestamp FROM orders ORDER BY id DESC"
        ).fetchall()
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["id", "order_number", "status", "subtotal", "discount", "tax", "total", "tendered", "change", "timestamp"])
        for r in rows:
            w.writerow([r["id"], r["order_number"], r["status"], r["subtotal"], r["discount_amount"], r["tax_amount"], r["total"], r["tendered"], r["change"], r["timestamp"]])
        return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=coffee-orders.csv"})

    # ============ CONSUMER MENU (public view) ============

    @app.get("/menu")
    def menu():
        db = get_db()
        sections = db.execute(
            "SELECT c.id, c.name, "
            "(SELECT COUNT(*) FROM products p WHERE p.category_id=c.id AND p.active=1) as n "
            "FROM categories c WHERE EXISTS (SELECT 1 FROM products p WHERE p.category_id=c.id AND p.active=1) "
            "ORDER BY c.sort_order"
        ).fetchall()
        items = db.execute(
            "SELECT p.id, p.name, p.price, p.stock_qty, p.description, p.featured, c.name as cat, c.id as cid "
            "FROM products p JOIN categories c ON p.category_id=c.id "
            "WHERE p.active=1 ORDER BY c.sort_order, p.name"
        ).fetchall()
        grouped = {}
        for it in items:
            grouped.setdefault(it["cid"], []).append(it)
        return render_template("menu.html", sections=sections, grouped=grouped)

    # ============ PRODUCT & CATEGORY MANAGEMENT ============

    def _parse_price_dollars(raw):
        try:
            cents = int(round(float(raw) * 100))
        except (ValueError, TypeError):
            return None
        return cents if cents > 0 else None

    def _product_form_fields():
        name = (request.form.get("name") or "").strip()[:60]
        try:
            category_id = int(request.form.get("category_id", 0))
        except (ValueError, TypeError):
            category_id = 0
        price = _parse_price_dollars(request.form.get("price_dollars", ""))
        try:
            stock_qty = max(0, min(9999, int(request.form.get("stock_qty", 0))))
        except (ValueError, TypeError):
            stock_qty = None
        try:
            low = max(0, min(999, int(request.form.get("low_stock_threshold", 5))))
        except (ValueError, TypeError):
            low = None
        active = 1 if request.form.get("active") == "on" else 0
        description = (request.form.get("description") or "").strip()[:200]
        featured = 1 if request.form.get("featured") == "on" else 0
        return name, category_id, price, stock_qty, low, active, description, featured

    def _product_form_valid(db, name, category_id, price, stock_qty, low):
        if not name:
            flash("Product name is required", "error")
            return False
        if not db.execute("SELECT 1 FROM categories WHERE id=?", (category_id,)).fetchone():
            flash("Choose a valid category", "error")
            return False
        if price is None:
            flash("Price must be a positive amount", "error")
            return False
        if stock_qty is None:
            flash("Stock must be a number", "error")
            return False
        if low is None:
            flash("Low-stock threshold must be a number", "error")
            return False
        return True

    @app.post("/products/add")
    def products_add():
        db = get_db()
        name, category_id, price, stock_qty, low, active, description, featured = _product_form_fields()
        if not _product_form_valid(db, name, category_id, price, stock_qty, low):
            return redirect(url_for("products"))
        db.execute(
            "INSERT INTO products (category_id, name, price, active, stock_tracked, stock_qty, low_stock_threshold, description, featured) VALUES (?,?,?,?,1,?,?,?,?)",
            (category_id, name, price, active, stock_qty, low, description, featured),
        )
        db.commit()
        flash("Product %s added" % name, "success")
        return redirect(url_for("products"))

    @app.post("/products/edit")
    def products_edit():
        db = get_db()
        try:
            pid = int(request.form.get("product_id", 0))
        except (ValueError, TypeError):
            return error("product_id required", 400)
        row = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not row:
            return error("Product not found", 404)
        name, category_id, price, stock_qty, low, active, description, featured = _product_form_fields()
        if not _product_form_valid(db, name, category_id, price, stock_qty, low):
            return redirect(url_for("products"))
        db.execute(
            "UPDATE products SET name=?, category_id=?, price=?, stock_qty=?, low_stock_threshold=?, active=?, description=?, featured=? WHERE id=?",
            (name, category_id, price, stock_qty, low, active, description, featured, pid),
        )
        db.commit()
        flash("Product %s updated" % name, "success")
        return redirect(url_for("products"))

    @app.post("/products/delete")
    def products_delete():
        db = get_db()
        try:
            pid = int(request.form.get("product_id", 0))
        except (ValueError, TypeError):
            return error("product_id required", 400)
        row = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not row:
            return error("Product not found", 404)
        try:
            db.execute("DELETE FROM cart_items WHERE product_id=?", (pid,))
            db.execute("DELETE FROM product_modifiers WHERE product_id=?", (pid,))
            db.execute("DELETE FROM products WHERE id=?", (pid,))
            db.commit()
            flash("Product %s deleted" % row["name"], "success")
        except sqlite3.IntegrityError:
            db.execute("ROLLBACK")
            flash("Product has sales history and cannot be deleted — set it inactive instead", "error")
        return redirect(url_for("products"))

    @app.post("/categories/add")
    def categories_add():
        db = get_db()
        name = (request.form.get("name") or "").strip()[:40]
        if not name:
            flash("Category name is required", "error")
            return redirect(url_for("products"))
        try:
            sort_order = int(request.form.get("sort_order", 0))
        except (ValueError, TypeError):
            sort_order = 0
        row = db.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM categories").fetchone()
        if sort_order <= 0:
            sort_order = row[0]
        db.execute("INSERT INTO categories (name, sort_order) VALUES (?,?)", (name, sort_order))
        db.commit()
        flash("Category %s added" % name, "success")
        return redirect(url_for("products"))

    @app.post("/categories/delete")
    def categories_delete():
        db = get_db()
        try:
            cid = int(request.form.get("category_id", 0))
        except (ValueError, TypeError):
            return error("category_id required", 400)
        row = db.execute("SELECT * FROM categories WHERE id=?", (cid,)).fetchone()
        if not row:
            return error("Category not found", 404)
        try:
            db.execute("DELETE FROM categories WHERE id=?", (cid,))
            db.commit()
            flash("Category %s deleted" % row["name"], "success")
        except sqlite3.IntegrityError:
            db.execute("ROLLBACK")
            flash("Category still has products — move or delete them first", "error")
        return redirect(url_for("products"))

    # ============ ORDER HOLDS (pending tabs) ============

    @app.post("/hold-order")
    def hold_order():
        db = get_db()
        cart = get_active_cart(db)
        if not cart:
            return error("Cart is empty", 400)
        cart_items, subtotal, tax = compute_cart_totals(db, cart["id"])
        if not cart_items:
            return error("Cart is empty", 400)
        try:
            db.execute("BEGIN IMMEDIATE")
            pref_row = db.execute("SELECT order_prefix FROM settings WHERE id=1").fetchone()
            prefix = pref_row["order_prefix"] if pref_row and pref_row["order_prefix"] else "R-"
            order_number = next_order_number(db, prefix)
            db.execute(
                "INSERT INTO orders(order_number,status,subtotal,discount_amount,tax_amount,total,tendered,change) VALUES (?,?,?,?,?,?,?,?)",
                (order_number, "pending", subtotal, 0, tax, subtotal + tax, 0, 0),
            )
            order_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            for item in cart_items:
                db.execute(
                    "INSERT INTO order_items(order_id,product_id,qty,unit_price,modifier_name,modifier_price_delta) VALUES (?,?,?,?,?,?)",
                    (order_id, item["product_id"], item["qty"], item["price"], "", 0),
                )
            db.execute("DELETE FROM cart_items WHERE cart_id=?", (cart["id"],))
            db.execute("UPDATE carts SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (cart["id"],))
            db.commit()
            flash("Order %s held — resume it anytime from Orders" % order_number, "success")
            return redirect(url_for("pos"))
        except Exception as e:
            db.execute("ROLLBACK")
            return error("Hold failed: %s" % str(e), 400)

    def _delete_pending_order(db, order_id):
        db.execute("DELETE FROM order_items WHERE order_id=?", (order_id,))
        db.execute("DELETE FROM orders WHERE id=? AND status='pending'", (order_id,))

    @app.post("/resume-hold")
    def resume_hold():
        db = get_db()
        try:
            order_id = int(request.form.get("order_id", 0))
        except (ValueError, TypeError):
            return error("order_id required", 400)
        order = db.execute("SELECT * FROM orders WHERE id=? AND status='pending'", (order_id,)).fetchone()
        if not order:
            return error("Pending order not found", 404)
        cart = get_or_create_cart(db)
        items = db.execute("SELECT product_id, qty FROM order_items WHERE order_id=?", (order_id,)).fetchall()
        for oi in items:
            prod = db.execute("SELECT * FROM products WHERE id=?", (oi["product_id"],)).fetchone()
            if not prod or prod["active"] == 0:
                continue
            existing = db.execute(
                "SELECT * FROM cart_items WHERE cart_id=? AND product_id=?", (cart["id"], oi["product_id"])
            ).fetchone()
            new_qty = min(99, (existing["qty"] if existing else 0) + oi["qty"])
            if existing:
                db.execute("UPDATE cart_items SET qty=? WHERE id=?", (new_qty, existing["id"]))
            else:
                db.execute("INSERT INTO cart_items (cart_id, product_id, qty) VALUES (?,?,?)", (cart["id"], oi["product_id"], new_qty))
        _delete_pending_order(db, order_id)
        db.commit()
        flash("Hold resumed into cart", "success")
        return redirect(url_for("pos"))

    @app.post("/discard-hold")
    def discard_hold():
        db = get_db()
        try:
            order_id = int(request.form.get("order_id", 0))
        except (ValueError, TypeError):
            return error("order_id required", 400)
        order = db.execute("SELECT * FROM orders WHERE id=? AND status='pending'", (order_id,)).fetchone()
        if not order:
            return error("Pending order not found", 404)
        _delete_pending_order(db, order_id)
        db.commit()
        flash("Hold %s discarded" % order["order_number"], "success")
        return redirect(url_for("orders"))

    with app.app_context():
        init_db()
    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5000)
