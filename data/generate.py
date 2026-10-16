"""
Makes a synthetic dataset for a small CA firm's client portfolio.

Everything here is fake. Names, PANs, GSTINs and accounts are made up.
Most of the data is normal business: suppliers sell to buyers, buyers pay.
On top of that we plant 4 fraud patterns that an auditor would want to catch:

  1. GST invoice ring    - shell companies at one address invoice each other in a loop
  2. Hidden related party - a client pays a vendor run by its director's brother-in-law,
                            and the money comes back to the director
  3. Layering            - one big payment split into 5 smaller ones through "mule"
                            accounts, then joined again at a company run by the sender's wife
  4. Hub account         - a payment "service" account that many unrelated firms route money through

Run:  python data/generate.py      -> writes data/seed.json
"""

import json
import random
from datetime import date, timedelta
from pathlib import Path

random.seed(42)  # same data every time, so the demo is repeatable

FY_START = date(2026, 4, 1)

FIRST = ["Amit", "Neha", "Rohan", "Priya", "Vikram", "Sneha", "Karan", "Pooja", "Arjun", "Meera",
         "Rahul", "Divya", "Sanjay", "Kavita", "Manish", "Ritu", "Nikhil", "Anjali", "Deepak", "Shalini",
         "Harsh", "Isha", "Gaurav", "Tanvi", "Varun", "Nisha", "Alok", "Swati", "Kunal", "Rekha"]
LAST = ["Patel", "Shah", "Iyer", "Reddy", "Gupta", "Joshi", "Nair", "Desai", "Kulkarni", "Agarwal",
        "Menon", "Bhatt", "Rao", "Chopra", "Verma", "Pillai", "Saxena", "Trivedi", "Bansal", "Mishra"]
CITIES = ["Ahmedabad", "Mumbai", "Pune", "Bengaluru", "Surat", "Jaipur", "Indore", "Vadodara"]
BANKS = ["HDFC Bank", "ICICI Bank", "SBI", "Axis Bank", "Kotak Bank", "Bank of Baroda"]
NORMAL_COMPANIES = [
    "Apex Polymers", "Bluewave Logistics", "Crestline Foods", "Deccan Steel Works", "Evergreen Agro",
    "Falcon Auto Parts", "Ganga Packaging", "Horizon Pharma", "Indus Electricals", "Jade Ceramics",
    "Kaveri Spinning Mills", "Lotus Chemicals", "Monsoon Textiles", "Nirmal Paper Products",
    "Orbit Tools", "Pinnacle Plastics", "Quartz Glassware", "Radiant Lighting", "Sapphire Garments",
    "Trident Hardware", "Unity Dairy", "Vertex Cables", "Woodland Furniture", "Yamuna Rice Mills",
]

data = {"persons": [], "companies": [], "accounts": [], "addresses": [],
        "director_of": [], "relative_of": [], "registered_at": [], "holds": [],
        "transfers": [], "invoices": []}

_counters = {}


def next_id(prefix):
    _counters[prefix] = _counters.get(prefix, 0) + 1
    return f"{prefix}{_counters[prefix]:03d}"


def fake_pan():
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return "".join(random.choices(letters, k=5)) + f"{random.randint(1000, 9999)}" + random.choice(letters)


def add_person(name, city):
    pid = next_id("P")
    data["persons"].append({"id": pid, "name": name, "pan": fake_pan(), "city": city})
    return pid


def add_address(line, city):
    aid = next_id("ADDR")
    data["addresses"].append({"id": aid, "name": f"{line}, {city}", "city": city})
    return aid


def add_company(name, city, address_id, incorporated, client=False):
    cid = next_id("C")
    gstin = f"24{fake_pan()}1Z{random.randint(1, 9)}"
    data["companies"].append({"id": cid, "name": name, "gstin": gstin, "city": city,
                              "incorporated_on": incorporated.isoformat(), "is_client": client})
    data["registered_at"].append({"from": cid, "to": address_id})
    return cid


def add_account(holder_id, holder_name):
    acc = next_id("A")
    bank = random.choice(BANKS)
    data["accounts"].append({"id": acc, "name": f"{holder_name} / {bank}", "bank": bank,
                             "number": f"XXXX{random.randint(1000, 9999)}"})
    data["holds"].append({"from": holder_id, "to": acc})
    return acc


def director(person_id, company_id, since="2019-04-01"):
    data["director_of"].append({"from": person_id, "to": company_id, "since": since})


def relative(a, b, relation):
    data["relative_of"].append({"from": a, "to": b, "relation": relation})


def transfer(src, dst, amount, day, ref):
    data["transfers"].append({"from": src, "to": dst, "amount": round(amount),
                              "date": day.isoformat(), "ref": ref})


def invoice(seller, buyer, amount, day):
    no = next_id("INV")
    data["invoices"].append({"from": seller, "to": buyer, "invoice_no": no, "amount": round(amount),
                             "gst": round(amount * 0.18), "date": day.isoformat()})
    return no


def rand_day(start_days=0, end_days=180):
    return FY_START + timedelta(days=random.randint(start_days, end_days))


def rand_name():
    return f"{random.choice(FIRST)} {random.choice(LAST)}"


# ---------------------------------------------------------------- normal business
normal = []  # list of (company_id, account_id, name)
for i, cname in enumerate(NORMAL_COMPANIES):
    city = random.choice(CITIES)
    addr = add_address(f"Plot {random.randint(1, 200)}, Industrial Estate Phase {random.randint(1, 4)}", city)
    cid = add_company(f"{cname} Pvt Ltd", city, addr,
                      date(random.randint(2005, 2020), random.randint(1, 12), random.randint(1, 28)),
                      client=(i % 3 == 0))
    for _ in range(random.randint(1, 2)):
        pid = add_person(rand_name(), city)
        director(pid, cid)
        if random.random() < 0.5:
            add_account(pid, data["persons"][-1]["name"])
    acc = add_account(cid, f"{cname} Pvt Ltd")
    normal.append((cid, acc, cname))

# A few family links among normal directors (normal families exist too)
people = [p["id"] for p in data["persons"]]
for _ in range(6):
    a, b = random.sample(people, 2)
    relative(a, b, random.choice(["spouse", "sibling", "cousin"]))

# Suppliers (lower index) sell to buyers (higher index). One direction only,
# so normal trade never forms a loop. Loops only come from planted fraud.
for _ in range(140):
    i, j = sorted(random.sample(range(len(normal)), 2))
    seller, buyer = normal[i], normal[j]
    amt = random.randint(80_000, 18_00_000)
    day = rand_day()
    no = invoice(seller[0], buyer[0], amt, day)
    # buyer pays seller (incl. GST), usually within 30 days
    transfer(buyer[1], seller[1], amt * 1.18, day + timedelta(days=random.randint(5, 30)), f"PAY {no}")

# Salaries / drawings: companies pay their own directors. Normal, small, monthly-ish.
for cid, acc, _ in normal:
    dirs = [d["from"] for d in data["director_of"] if d["to"] == cid]
    for pid in dirs:
        pacc = [h["to"] for h in data["holds"] if h["from"] == pid]
        if pacc:
            for m in range(0, 6, 2):
                transfer(acc, pacc[0], random.randint(60_000, 1_50_000), FY_START + timedelta(days=30 * m + 28), "SALARY")

# ---------------------------------------------------------------- pattern 1: GST invoice ring
ring_addr = add_address("Shop 14, Ring Road Textile Market", "Surat")
ring_names = ["Shreeji Impex", "Balaji Tradelink", "Om Sai Enterprises", "Maruti Commodities"]
ring = []
ring_people = []
for n in ring_names:
    cid = add_company(f"{n}", "Surat", ring_addr, date(2026, 2, random.randint(1, 25)))
    pid = add_person(rand_name(), "Surat")
    ring_people.append(pid)
    director(pid, cid, "2026-02-01")
    acc = add_account(cid, n)
    ring.append((cid, acc, n))
relative(ring_people[0], ring_people[2], "brother")
relative(ring_people[1], ring_people[3], "cousin")

# Three rounds of the same goods "sold" around the loop, with almost no real margin
for rnd in range(3):
    base = FY_START + timedelta(days=40 + rnd * 45)
    amt = random.randint(46_00_000, 50_00_000)
    for k in range(4):
        s, b = ring[k], ring[(k + 1) % 4]
        d = base + timedelta(days=k * 2)
        amt = amt * 1.01
        no = invoice(s[0], b[0], amt, d)
        transfer(b[1], s[1], amt * 1.18, d + timedelta(days=1), f"PAY {no}")

# The ring then sells "on paper" to a real client, who claims GST input credit on it
victim = normal[3]  # Deccan Steel Works (a client)
for k in range(2):
    d = FY_START + timedelta(days=150 + k * 10)
    amt = random.randint(30_00_000, 35_00_000)
    no = invoice(ring[0][0], victim[0], amt, d)
    transfer(victim[1], ring[0][1], amt * 1.18, d + timedelta(days=3), f"PAY {no}")

# ---------------------------------------------------------------- pattern 2: hidden related party
st_addr = add_address("12, Ashram Road", "Ahmedabad")
sharma = add_company("Sharma Traders Pvt Ltd", "Ahmedabad", st_addr, date(2012, 6, 14), client=True)
rajesh = add_person("Rajesh Sharma", "Ahmedabad")
director(rajesh, sharma, "2012-06-14")
sharma_acc = add_account(sharma, "Sharma Traders Pvt Ltd")
rajesh_acc = add_account(rajesh, "Rajesh Sharma")

ky_addr = add_address("B-7, Naroda GIDC", "Ahmedabad")
kavya = add_company("Kavya Supplies", "Ahmedabad", ky_addr, date(2025, 11, 2))
anil = add_person("Anil Kapoor", "Ahmedabad")
director(anil, kavya, "2025-11-02")
kavya_acc = add_account(kavya, "Kavya Supplies")
relative(rajesh, anil, "brother-in-law")

# Sharma Traders also does normal business with normal companies
for _ in range(8):
    sup = random.choice(normal)
    amt = random.randint(2_00_000, 9_00_000)
    d = rand_day()
    no = invoice(sup[0], sharma, amt, d)
    transfer(sharma_acc, sup[1], amt * 1.18, d + timedelta(days=12), f"PAY {no}")

# Big "purchases" from Kavya, and most of the money flows back to Rajesh within days
for m in range(6):
    d = FY_START + timedelta(days=15 + m * 30)
    amt = random.randint(25_00_000, 32_00_000)
    no = invoice(kavya, sharma, amt, d)
    transfer(sharma_acc, kavya_acc, amt * 1.18, d + timedelta(days=4), f"PAY {no}")
    transfer(kavya_acc, rajesh_acc, amt * 0.9, d + timedelta(days=7), "LOAN")

# ---------------------------------------------------------------- pattern 3: layering
ns_addr = add_address("Tower B, SG Highway", "Ahmedabad")
northstar = add_company("Northstar Infra Pvt Ltd", "Ahmedabad", ns_addr, date(2015, 3, 9), client=True)
vikas = add_person("Vikas Malhotra", "Ahmedabad")
director(vikas, northstar, "2015-03-09")
ns_acc = add_account(northstar, "Northstar Infra Pvt Ltd")

zh_addr = add_address("Office 402, Prahlad Nagar", "Ahmedabad")
zenith = add_company("Zenith Holdings LLP", "Ahmedabad", zh_addr, date(2024, 8, 19))
sunita = add_person("Sunita Malhotra", "Ahmedabad")
director(sunita, zenith, "2024-08-19")
zh_acc = add_account(zenith, "Zenith Holdings LLP")
relative(vikas, sunita, "spouse")

mules = []
for _ in range(5):
    name = rand_name()
    pid = add_person(name, "Ahmedabad")
    mules.append(add_account(pid, name))

# Normal work for Northstar too
for _ in range(6):
    sup = random.choice(normal)
    amt = random.randint(3_00_000, 12_00_000)
    d = rand_day()
    no = invoice(sup[0], northstar, amt, d)
    transfer(ns_acc, sup[1], amt * 1.18, d + timedelta(days=10), f"PAY {no}")

# 95 lakh split into 5 x 19 lakh "advances", all rejoining at Zenith within 2 days
d = FY_START + timedelta(days=121)
for k, m in enumerate(mules):
    transfer(ns_acc, m, 19_00_000, d, f"ADVANCE {k + 1}")
    transfer(m, zh_acc, 18_90_000, d + timedelta(days=random.randint(1, 2)), "INVESTMENT")

# ---------------------------------------------------------------- pattern 4: hub account
qp_addr = add_address("Unit 9, Andheri East", "Mumbai")
quickpay = add_company("QuickPay Collections", "Mumbai", qp_addr, date(2025, 12, 5))
qp_dir = add_person(rand_name(), "Mumbai")
director(qp_dir, quickpay, "2025-12-05")
hub = add_account(quickpay, "QuickPay Collections")

senders = random.sample(normal, 12)
receivers = [m for m in normal if m not in senders][:6]
for s in senders:
    d = rand_day(60, 170)
    transfer(s[1], hub, random.randint(4_00_000, 9_00_000), d, "SERVICE CHARGES")
for r in receivers:
    for _ in range(2):
        d = rand_day(60, 175)
        transfer(hub, r[1], random.randint(5_00_000, 11_00_000), d, "SETTLEMENT")

# ---------------------------------------------------------------- write
out = Path(__file__).parent / "seed.json"
out.write_text(json.dumps(data, indent=1))
print(f"wrote {out}")
for k, v in data.items():
    print(f"  {k:14s} {len(v)}")
