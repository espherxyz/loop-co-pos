"""
CoffeeShopPOS / Loop Co — idempotent database seeder.
Run: python seed.py
Loads shop settings, categories, and the menu into coffee.sqlite3.
Safe to run repeatedly (INSERT OR IGNORE keyed on fixed IDs).
Kept out of schema.sql so test databases (created via app.init_db())
start empty and tests can insert their own fixtures.
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "coffee.sqlite3"

SETTINGS = (1, "Loop Co", "₱", 0.08)  # id, shop_name, currency_symbol, tax_rate

CATEGORIES = [
    (1, "Espresso Bar", 1),
    (2, "Brewed & Tea", 2),
    (3, "Bakery", 3),
]

# (id, category_id, name, price_cents, stock_qty, low_stock_threshold, description, featured)
PRODUCTS = [
    (1, 1, "Espresso", 250, 100, 10,
     "Our signature blend - bold, velvety, a double shot of pure focus.", 0),
    (2, 1, "Latte", 450, 80, 10,
     "Silky steamed milk over a double shot, finished with latte art.", 1),
    (3, 1, "Cappuccino", 400, 80, 10,
     "Equal parts espresso, steamed milk and velvet foam.", 0),
    (4, 1, "Americano", 350, 90, 10,
     "Clean and bright - espresso lengthened the classic way.", 0),
    (5, 1, "Cold Brew", 400, 40, 10,
     "Steeped slow for 18 hours. Smooth, bold, zero bitterness.", 1),
    (6, 2, "Drip Coffee", 300, 100, 10,
     "Today's house roast, brewed fresh and always poured warm.", 0),
    (7, 2, "Green Tea", 350, 60, 10,
     "Loose-leaf sencha - grassy, calm, quietly caffeinated.", 0),
    (8, 2, "Chai Latte", 420, 40, 10,
     "Spiced black tea folded into steamed milk.", 0),
    (9, 3, "Butter Croissant", 380, 30, 5,
     "Laminated over three days, baked till it shatters.", 1),
    (10, 3, "Banana Bread", 320, 20, 5,
     "Toasted, with whipped butter. Tastes like home.", 0),
    (11, 3, "Almond Biscotti", 280, 25, 5,
     "Twice-baked and dunk-approved.", 0),
    (12, 3, "Cinnamon Roll", 450, 15, 5,
     "Pillowy swirls under a brown-butter glaze.", 0),
]


def main():
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA foreign_keys=ON")
    schema = (Path(__file__).resolve().parent / "schema.sql").read_text(encoding="utf-8")
    db.executescript(schema)

    db.execute(
        "INSERT OR IGNORE INTO settings (id, shop_name, currency_symbol, tax_rate) VALUES (?,?,?,?)",
        SETTINGS,
    )
    for row in CATEGORIES:
        db.execute("INSERT OR IGNORE INTO categories (id, name, sort_order) VALUES (?,?,?)", row)
    for pid, cat_id, name, price, stock, low, desc, feat in PRODUCTS:
        db.execute(
            "INSERT OR IGNORE INTO products (id, category_id, name, price, active, stock_tracked, stock_qty, low_stock_threshold, description, featured) "
            "VALUES (?,?,?,?,1,1,?,?,?,?)",
            (pid, cat_id, name, price, stock, low, desc, feat),
        )
    db.commit()

    n_cat = db.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
    n_prod = db.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    s = db.execute("SELECT shop_name, currency_symbol, tax_rate FROM settings WHERE id=1").fetchone()
    print(f"Seed complete: shop={s[0]!r} currency={s[1]!r} tax={s[2]} categories={n_cat} products={n_prod}")
    db.close()


if __name__ == "__main__":
    main()
