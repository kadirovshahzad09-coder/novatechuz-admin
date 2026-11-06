import os, json, sqlite3, hashlib, hmac, secrets, time, threading, io, re, urllib.request, urllib.parse, math
import qrcode
import qrcode.image.svg
from reportlab.graphics.barcode import createBarcodeDrawing
from reportlab.graphics import renderSVG
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from pathlib import Path
ROOT=Path(__file__).resolve().parent
DB=Path(os.getenv('NOVATECH_DB',str(ROOT/'novatech.sqlite3')))
USER=os.getenv('NOVATECH_ADMIN_USER','')
PASSWORD=os.getenv('NOVATECH_ADMIN_PASSWORD','')
TELEGRAM_TOKEN=os.getenv('NOVATECH_TELEGRAM_BOT_TOKEN','')
TELEGRAM_CHAT=os.getenv('NOVATECH_TELEGRAM_CHAT_ID','')
SESSIONS={}
LOCK=threading.Lock()
def connect():
 c=sqlite3.connect(DB,timeout=10); c.row_factory=sqlite3.Row; c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA foreign_keys=ON'); return c
def init():
 with connect() as c:
  c.executescript('''CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,category TEXT NOT NULL DEFAULT 'Boshqa',sku TEXT NOT NULL UNIQUE,price_usd REAL NOT NULL CHECK(price_usd>=0),quantity INTEGER NOT NULL DEFAULT 0 CHECK(quantity>=0),ram_options TEXT NOT NULL DEFAULT '[]',ssd_options TEXT NOT NULL DEFAULT '[]',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP); CREATE TABLE IF NOT EXISTS sales(id INTEGER PRIMARY KEY AUTOINCREMENT,product_id INTEGER NOT NULL REFERENCES products(id),quantity INTEGER NOT NULL CHECK(quantity>0),unit_price_usd REAL NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP); CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT,product_id INTEGER NOT NULL REFERENCES products(id),quantity INTEGER NOT NULL,ram TEXT NOT NULL DEFAULT '',ssd TEXT NOT NULL DEFAULT '',unit_price_usd REAL NOT NULL,total_usd REAL NOT NULL,status TEXT NOT NULL DEFAULT 'new',telegram_status TEXT NOT NULL DEFAULT 'not_configured',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP); CREATE TABLE IF NOT EXISTS stock_movements(id INTEGER PRIMARY KEY AUTOINCREMENT,product_id INTEGER NOT NULL REFERENCES products(id),delta INTEGER NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);''')
def migrate_orders():
 with connect() as c:
  columns={r['name'] for r in c.execute('PRAGMA table_info(orders)')}
  if 'sale_id' not in columns:c.execute('ALTER TABLE orders ADD COLUMN sale_id INTEGER REFERENCES sales(id)')
  c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_sale_id ON orders(sale_id) WHERE sale_id IS NOT NULL")

def migrate_finance():
 with connect() as c:
  cols={r['name'] for r in c.execute('PRAGMA table_info(products)')}
  if 'cost_usd' not in cols:c.execute('ALTER TABLE products ADD COLUMN cost_usd REAL')
  cols={r['name'] for r in c.execute('PRAGMA table_info(sales)')}
  if 'unit_cost_usd' not in cols:c.execute('ALTER TABLE sales ADD COLUMN unit_cost_usd REAL')
  c.execute('CREATE TABLE IF NOT EXISTS expenses(id INTEGER PRIMARY KEY AUTOINCREMENT,description TEXT NOT NULL,amount_usd REAL NOT NULL CHECK(amount_usd>0),created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
def migrate_debts():
 with connect() as c:
  c.executescript("""CREATE TABLE IF NOT EXISTS debts(
   id INTEGER PRIMARY KEY AUTOINCREMENT,person TEXT NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('receivable','payable')),
   principal_usd REAL NOT NULL CHECK(principal_usd>0),due_date TEXT NOT NULL,
   note TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
   CREATE TABLE IF NOT EXISTS debt_payments(
   id INTEGER PRIMARY KEY AUTOINCREMENT,debt_id INTEGER NOT NULL REFERENCES debts(id),
   amount_usd REAL NOT NULL CHECK(amount_usd>0),created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
   CREATE INDEX IF NOT EXISTS debt_payments_debt_id ON debt_payments(debt_id);""")
def money(v,allow_zero=True):
 n=float(v)
 if not math.isfinite(n) or n<0 or (not allow_zero and n==0) or n>100000000:raise ValueError()
 return round(n,2)
def validate_product(data):
 name=str(data['name']).strip();sku=str(data['sku']).strip();category=str(data.get('category','Boshqa')).strip()
 price=money(data['price_usd']);cost=money(data['cost_usd']) if data.get('cost_usd') not in (None,'') else None;ram=data.get('ram_options',[]);ssd=data.get('ssd_options',[])
 if not name or not sku or len(name)>200 or len(sku)>100 or len(category)>100 or not category or not (0<=price<=100000000) or not isinstance(ram,list) or not isinstance(ssd,list):raise ValueError()
 for options in (ram,ssd):
  if len(options)>30:raise ValueError()
  for x in options:
   if not isinstance(x,dict) or not str(x.get('name','')).strip() or len(str(x['name']))>50 or not (0<=float(x['price'])<=10000000):raise ValueError()
 return name,sku,category,price,json.dumps(ram),json.dumps(ssd),cost

def notify_telegram(message):
 if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:return 'not_configured'
 try:
  payload=urllib.parse.urlencode({'chat_id':TELEGRAM_CHAT,'text':message}).encode()
  request=urllib.request.Request('https://api.telegram.org/bot'+TELEGRAM_TOKEN+'/sendMessage',data=payload,method='POST')
  with urllib.request.urlopen(request,timeout=7) as response:
   result=json.load(response)
  return 'sent' if result.get('ok') else 'failed'
 except Exception:return 'failed'

class Handler(BaseHTTPRequestHandler):
 def send_json(self,status,data,headers=None):
  b=json.dumps(data,ensure_ascii=False).encode();self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(b)))
  for k,v in (headers or {}).items():self.send_header(k,v)
  self.end_headers();self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0'))
  if n>100000:self.send_json(413,{'error':'So‘rov juda katta'});return None
  try:return json.loads(self.rfile.read(n))
  except (ValueError,UnicodeDecodeError):self.send_json(400,{'error':'JSON noto‘g‘ri'});return None
 def authorized(self):
  token=self.headers.get('Authorization','').removeprefix('Bearer ').strip()
  with LOCK:
   expiry=SESSIONS.get(token,0)
   if expiry>time.time():return True
   SESSIONS.pop(token,None)
  self.send_json(401,{'error':'Avtorizatsiya talab qilinadi'});return False
 def do_GET(self):
  path=urlparse(self.path).path
  if path=='/api/health':return self.send_json(200,{'status':'ok'})
  if path=='/api/catalog':
   with connect() as c: rows=[dict(r) for r in c.execute('SELECT id,name,category,sku,price_usd,quantity,ram_options,ssd_options FROM products WHERE quantity>0 ORDER BY id DESC')]
   for r in rows:r['ram_options']=json.loads(r['ram_options']);r['ssd_options']=json.loads(r['ssd_options'])
   return self.send_json(200,{'products':rows})
  if path=='/api/products':
   if not self.authorized():return
   with connect() as c: rows=[dict(r) for r in c.execute('SELECT * FROM products ORDER BY id DESC')]
   for r in rows:r['ram_options']=json.loads(r['ram_options']);r['ssd_options']=json.loads(r['ssd_options'])
   return self.send_json(200,{'products':rows})
  if path=='/api/movements':
   if not self.authorized():return
   with connect() as c: rows=[dict(r) for r in c.execute('SELECT m.*,p.name FROM stock_movements m JOIN products p ON p.id=m.product_id ORDER BY m.id DESC LIMIT 200')]
   return self.send_json(200,{'movements':rows})
  if path=='/api/orders':
   if not self.authorized():return
   with connect() as c: rows=[dict(r) for r in c.execute('SELECT o.*,p.name,p.sku FROM orders o JOIN products p ON p.id=o.product_id ORDER BY o.id DESC LIMIT 300')]
   return self.send_json(200,{'orders':rows})
  if path=='/api/reports':
   if not self.authorized():return self.send_json(401,{'error':'Avtorizatsiya talab qilinadi'})
   with connect() as c:
    daily=[dict(r) for r in c.execute("SELECT date(created_at) period,COUNT(*) transactions,SUM(quantity) units,ROUND(SUM(quantity*unit_price_usd),2) revenue_usd FROM sales WHERE created_at>=datetime('now','-30 days') GROUP BY date(created_at) ORDER BY period DESC")]
    monthly=[dict(r) for r in c.execute("SELECT substr(created_at,1,7) period,COUNT(*) transactions,SUM(quantity) units,ROUND(SUM(quantity*unit_price_usd),2) revenue_usd FROM sales GROUP BY substr(created_at,1,7) ORDER BY period DESC LIMIT 12")]
    top=[dict(r) for r in c.execute("SELECT p.name,p.sku,SUM(s.quantity) units,ROUND(SUM(s.quantity*s.unit_price_usd),2) revenue_usd FROM sales s JOIN products p ON p.id=s.product_id GROUP BY s.product_id ORDER BY units DESC,revenue_usd DESC LIMIT 10")]
    stock=dict(c.execute("SELECT COUNT(*) products,COALESCE(SUM(quantity),0) units,ROUND(COALESCE(SUM(quantity*price_usd),0),2) retail_value_usd FROM products").fetchone())
    orders=[dict(r) for r in c.execute("SELECT status,COUNT(*) count FROM orders GROUP BY status")]
   with connect() as c:
    f=c.execute('SELECT ROUND(COALESCE(SUM(quantity*unit_price_usd),0),2) revenue,ROUND(COALESCE(SUM(CASE WHEN unit_cost_usd IS NOT NULL THEN quantity*(unit_price_usd-unit_cost_usd) ELSE 0 END),0),2) known_margin,COUNT(*) total_sales,SUM(CASE WHEN unit_cost_usd IS NULL THEN 1 ELSE 0 END) unknown_sales FROM sales').fetchone()
    expense_total=c.execute('SELECT ROUND(COALESCE(SUM(amount_usd),0),2) FROM expenses').fetchone()[0]
    recent_expenses=[dict(r) for r in c.execute('SELECT * FROM expenses ORDER BY id DESC LIMIT 30')]
   finance={'revenue_usd':f['revenue'],'known_gross_margin_usd':f['known_margin'],'expenses_usd':expense_total,'estimated_net_usd':round(f['known_margin']-expense_total,2) if not f['unknown_sales'] else None,'sales_missing_cost':f['unknown_sales'] or 0,'expenses':recent_expenses}
   return self.send_json(200,{'finance':finance,'daily':daily,'monthly':monthly,'top_products':top,'stock':stock,'order_statuses':orders,'note':'Tushum sotuv yozuvlari asosida; foyda yoki naqd pul oqimi emas.'})
  if path=='/api/debts':
   if not self.authorized():return
   with connect() as c:
    rows=[dict(r) for r in c.execute("""SELECT d.*,ROUND(COALESCE(SUM(p.amount_usd),0),2) paid_usd,
     ROUND(d.principal_usd-COALESCE(SUM(p.amount_usd),0),2) balance_usd
     FROM debts d LEFT JOIN debt_payments p ON p.debt_id=d.id
     GROUP BY d.id ORDER BY (balance_usd>0) DESC,d.due_date ASC,d.id DESC""")]
    payments=[dict(r) for r in c.execute('SELECT * FROM debt_payments ORDER BY id DESC LIMIT 200')]
   return self.send_json(200,{'debts':rows,'payments':payments})
  if path=='/api/expenses':
   if not self.authorized():return
   with connect() as c: rows=[dict(r) for r in c.execute('SELECT * FROM expenses ORDER BY id DESC LIMIT 200')]
   return self.send_json(200,{'expenses':rows})
  if path=='/api/sales':
   if not self.authorized():return
   with connect() as c: rows=[dict(r) for r in c.execute('SELECT s.*,p.name,p.sku FROM sales s JOIN products p ON p.id=s.product_id ORDER BY s.id DESC LIMIT 500')]
   return self.send_json(200,{'sales':rows})
  if path in ('/','/admin-v3.html','/catalog-v1.html'):
   name='index.html' if path=='/' else path.lstrip('/');p=ROOT/'public'/name
   if p.is_file():
    b=p.read_bytes();self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(b)));self.end_headers();return self.wfile.write(b)
  return self.send_json(404,{'error':'Topilmadi'})
 def do_PUT(self):
  if urlparse(self.path).path!='/api/products':return self.send_json(404,{'error':'Topilmadi'})
  if not self.authorized():return
  data=self.body()
  if data is None:return
  try:
   pid=int(data['id']);name,sku,category,price,ram,ssd,cost=validate_product(data)
   with connect() as c:
    cur=c.execute('UPDATE products SET name=?,sku=?,category=?,price_usd=?,ram_options=?,ssd_options=?,cost_usd=? WHERE id=?',(name,sku,category,price,ram,ssd,cost,pid))
   if not cur.rowcount:return self.send_json(404,{'error':'Mahsulot topilmadi'})
   return self.send_json(200,{'ok':True})
  except (ValueError,TypeError,KeyError,OverflowError):return self.send_json(400,{'error':'Mahsulot ma’lumotlari noto‘g‘ri'})
  except sqlite3.IntegrityError:return self.send_json(409,{'error':'SKU takrorlangan'})
 def do_POST(self):
  path=urlparse(self.path).path;data=self.body()
  if data is None:return
  if path=='/api/login':
   u=str(data.get('username',''));p=str(data.get('password',''))
   if USER and PASSWORD and hmac.compare_digest(u,USER) and hmac.compare_digest(p,PASSWORD):
    token=secrets.token_urlsafe(32)
    with LOCK:SESSIONS[token]=time.time()+8*3600
    return self.send_json(200,{'token':token,'expires_in':28800})
   return self.send_json(401,{'error':'Login yoki parol noto‘g‘ri'})
  if path=='/api/orders':
   try:
    if not isinstance(data,dict):raise ValueError()
    pid=int(data['product_id']);qty=int(data.get('quantity',1))
    if qty<1 or qty>20:raise ValueError()
    selected={}
    for key in ('ram','ssd'):
     value=data.get(key,'')
     if not isinstance(value,str) or len(value)>50:raise ValueError()
     selected[key]=value
    with connect() as c:
     c.execute('BEGIN IMMEDIATE')
     product=c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()
     if product is None:return self.send_json(404,{'error':'Mahsulot topilmadi'})
     if product['quantity']<qty:return self.send_json(409,{'error':'Omborda yetarli mahsulot yo‘q'})
     unit=float(product['price_usd'])
     for key,column in [('ram','ram_options'),('ssd','ssd_options')]:
      choices=json.loads(product[column]);choice=selected[key]
      if choices:
       matches=[x for x in choices if x['name']==choice]
       if len(matches)!=1:raise ValueError()
       unit+=float(matches[0]['price'])
      elif choice:raise ValueError()
     if not math.isfinite(unit):raise ValueError()
     total=round(unit*qty,2)
     cur=c.execute('INSERT INTO orders(product_id,quantity,ram,ssd,unit_price_usd,total_usd) VALUES(?,?,?,?,?,?)',(pid,qty,selected['ram'],selected['ssd'],unit,total))
     oid=cur.lastrowid
    message=f"🛒 NovaTech-uz buyurtma #{oid}\nMahsulot: {product['name']}\nSKU: {product['sku']}\nRAM: {selected['ram'] or '-'}\nSSD: {selected['ssd'] or '-'}\nSoni: {qty}\nJami: ${total:.2f}\nHolat: tasdiqlanmagan"
    status=notify_telegram(message)
    with connect() as c:c.execute('UPDATE orders SET telegram_status=? WHERE id=?',(status,oid))
    return self.send_json(201,{'order_id':oid,'total_usd':total,'telegram_status':status,'message':'Buyurtma qabul qilindi, do‘kon tasdiqlashini kuting'})
   except (ValueError,TypeError,KeyError,OverflowError,sqlite3.Error):return self.send_json(400,{'error':'Buyurtma ma’lumotlarini tekshiring'})
  if not self.authorized():return
  if path=='/api/order-status':
   try:
    oid=int(data['order_id']);action=str(data['action'])
    if action not in ('confirm','cancel','sell'):raise ValueError()
    with connect() as c:
     c.execute('BEGIN IMMEDIATE')
     order=c.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
     if order is None:return self.send_json(404,{'error':'Buyurtma topilmadi'})
     current=order['status']
     if action=='confirm':
      if current!='new':return self.send_json(409,{'error':'Faqat yangi buyurtmani tasdiqlash mumkin'})
      c.execute("UPDATE orders SET status='confirmed' WHERE id=?",(oid,))
      return self.send_json(200,{'status':'confirmed'})
     if action=='cancel':
      if current not in ('new','confirmed'):return self.send_json(409,{'error':'Bu buyurtmani bekor qilib bo‘lmaydi'})
      c.execute("UPDATE orders SET status='cancelled' WHERE id=?",(oid,))
      return self.send_json(200,{'status':'cancelled'})
     if current!='confirmed':return self.send_json(409,{'error':'Sotishdan oldin buyurtmani tasdiqlang'})
     qty=order['quantity'];pid=order['product_id']
     updated=c.execute('UPDATE products SET quantity=quantity-? WHERE id=? AND quantity>=?',(qty,pid,qty))
     if updated.rowcount!=1:return self.send_json(409,{'error':'Omborda yetarli mahsulot yo‘q'})
     sale=c.execute('INSERT INTO sales(product_id,quantity,unit_price_usd,unit_cost_usd) VALUES(?,?,?,?)',(pid,qty,order['unit_price_usd'],c.execute('SELECT cost_usd FROM products WHERE id=?',(pid,)).fetchone()['cost_usd']))
     sid=sale.lastrowid
     c.execute('INSERT INTO stock_movements(product_id,delta,reason) VALUES(?,?,?)',(pid,-qty,'Buyurtma #'+str(oid)+' / savdo #'+str(sid)))
     c.execute("UPDATE orders SET status='sold',sale_id=? WHERE id=?",(sid,oid))
    return self.send_json(200,{'status':'sold','sale_id':sid})
   except (ValueError,TypeError,KeyError,OverflowError):return self.send_json(400,{'error':'Buyurtma amali noto‘g‘ri'})
  if path=='/api/label':
   try:
    pid=int(data['product_id'])
    with connect() as c:r=c.execute('SELECT name,sku,price_usd FROM products WHERE id=?',(pid,)).fetchone()
    if r is None:return self.send_json(404,{'error':'Mahsulot topilmadi'})
    sku=r['sku']
    if not (1<=len(sku)<=100) or not sku.isascii() or any(ord(ch)<32 or ord(ch)>126 for ch in sku):
     return self.send_json(422,{'error':'Shtrix kod uchun SKU oddiy lotin harflari, raqamlar va belgilardan iborat bo‘lishi kerak'})
    qr=qrcode.make('NOVATECH-UZ | SKU: '+sku, image_factory=qrcode.image.svg.SvgPathImage)
    buf=io.BytesIO();qr.save(buf)
    qr_svg=buf.getvalue().decode('utf-8');qr_svg=re.sub(r'<\?xml[^>]*\?>','',qr_svg).strip()
    drawing=createBarcodeDrawing('Code128',value=sku,barHeight=28,humanReadable=False)
    barcode_svg=renderSVG.drawToString(drawing)
    if isinstance(barcode_svg,bytes):barcode_svg=barcode_svg.decode('utf-8')
    barcode_svg=re.sub(r'<\?xml[^>]*\?>','',barcode_svg).strip()
    return self.send_json(200,{'name':r['name'],'sku':sku,'price_usd':r['price_usd'],'qr_svg':qr_svg,'barcode_svg':barcode_svg})
   except (ValueError,TypeError,KeyError):return self.send_json(400,{'error':'Mahsulot ID noto‘g‘ri'})
  if path=='/api/products':
   try:
    name,sku,category,price,ram,ssd,cost=validate_product(data);qty=int(data.get('quantity',0))
    if qty<0:raise ValueError()
    with connect() as c:
     cur=c.execute('INSERT INTO products(name,category,sku,price_usd,quantity,ram_options,ssd_options,cost_usd) VALUES(?,?,?,?,?,?,?,?)',(name,category,sku,price,qty,ram,ssd,cost))
    return self.send_json(201,{'id':cur.lastrowid})
   except (ValueError,TypeError,KeyError,OverflowError):return self.send_json(400,{'error':'Mahsulot ma’lumotlarini tekshiring'})
   except sqlite3.IntegrityError:return self.send_json(409,{'error':'SKU takrorlangan'})
  if path=='/api/stock':
   try:
    pid=int(data['product_id']);delta=int(data['delta'])
    if delta==0:raise ValueError()
    with connect() as c:
     c.execute('BEGIN IMMEDIATE');cur=c.execute('UPDATE products SET quantity=quantity+? WHERE id=? AND quantity+?>=0',(delta,pid,delta))
     if cur.rowcount!=1:return self.send_json(409,{'error':'Mahsulot yo‘q yoki qoldiq yetarli emas'})
     c.execute('INSERT INTO stock_movements(product_id,delta,reason) VALUES(?,?,?)',(pid,delta,'Qo‘lda tuzatish'))
    return self.send_json(200,{'ok':True})
   except (ValueError,TypeError,KeyError):return self.send_json(400,{'error':'Qoldiq ma’lumotlari noto‘g‘ri'})
  if path=='/api/debts':
   try:
    import datetime
    person=str(data['person']).strip();kind=str(data['kind']);principal=money(data['principal_usd'],False)
    due=str(data['due_date']);datetime.date.fromisoformat(due)
    note=str(data.get('note','')).strip()
    if not person or len(person)>120 or kind not in ('receivable','payable') or len(note)>500:raise ValueError()
    with connect() as c:cur=c.execute('INSERT INTO debts(person,kind,principal_usd,due_date,note) VALUES(?,?,?,?,?)',(person,kind,principal,due,note))
    return self.send_json(201,{'id':cur.lastrowid})
   except (ValueError,KeyError,TypeError,OverflowError):return self.send_json(400,{'error':'Qarz ma’lumotlari noto‘g‘ri'})
  if path=='/api/debt-payments':
   try:
    debt_id=int(data['debt_id']);amount=money(data['amount_usd'],False)
    with connect() as c:
     c.execute('BEGIN IMMEDIATE')
     row=c.execute("""SELECT d.principal_usd-COALESCE(SUM(p.amount_usd),0) balance
      FROM debts d LEFT JOIN debt_payments p ON p.debt_id=d.id WHERE d.id=? GROUP BY d.id""",(debt_id,)).fetchone()
     if row is None:return self.send_json(404,{'error':'Qarz topilmadi'})
     if round(row['balance'],2)+0.000001<amount:return self.send_json(409,{'error':'To‘lov qoldiqdan oshib ketdi'})
     cur=c.execute('INSERT INTO debt_payments(debt_id,amount_usd) VALUES(?,?)',(debt_id,amount))
    return self.send_json(201,{'id':cur.lastrowid})
   except (ValueError,KeyError,TypeError,OverflowError):return self.send_json(400,{'error':'To‘lov noto‘g‘ri'})
  if path=='/api/expenses':
   try:
    description=str(data['description']).strip();amount=money(data['amount_usd'],False)
    if not description or len(description)>250:raise ValueError()
    with connect() as c:cur=c.execute('INSERT INTO expenses(description,amount_usd) VALUES(?,?)',(description,amount))
    return self.send_json(201,{'id':cur.lastrowid})
   except (ValueError,TypeError,KeyError):return self.send_json(400,{'error':'Xarajat noto‘g‘ri'})
  if path=='/api/sales':
   try:
    pid=int(data['product_id']);qty=int(data['quantity']);
    if qty<=0:raise ValueError()
    with connect() as c:
     c.execute('BEGIN IMMEDIATE');r=c.execute('SELECT price_usd,quantity,cost_usd FROM products WHERE id=?',(pid,)).fetchone()
     if r is None:return self.send_json(404,{'error':'Mahsulot topilmadi'})
     if r['quantity']<qty:return self.send_json(409,{'error':'Omborda yetarli mahsulot yo‘q'})
     c.execute('UPDATE products SET quantity=quantity-? WHERE id=?',(qty,pid));cur=c.execute('INSERT INTO sales(product_id,quantity,unit_price_usd,unit_cost_usd) VALUES(?,?,?,?)',(pid,qty,r['price_usd'],r['cost_usd']))
     c.execute('INSERT INTO stock_movements(product_id,delta,reason) VALUES(?,?,?)',(pid,-qty,'Savdo #'+str(cur.lastrowid)))
    return self.send_json(201,{'sale_id':cur.lastrowid})
   except (ValueError,TypeError,KeyError):return self.send_json(400,{'error':'Savdo ma’lumotlari noto‘g‘ri'})
  return self.send_json(404,{'error':'Topilmadi'})
if __name__=='__main__':
 if not USER or not PASSWORD:raise SystemExit('NOVATECH_ADMIN_USER va NOVATECH_ADMIN_PASSWORD muhit o‘zgaruvchilarini o‘rnating')
 init();migrate_orders();migrate_finance();migrate_debts();port=int(os.getenv('PORT','8000'));print(f'NovaTech server http://127.0.0.1:{port}');ThreadingHTTPServer(('0.0.0.0',port),Handler).serve_forever()
