import os, secrets, hashlib, hmac, smtplib, csv, io
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import create_engine, String, Integer, Boolean, Date, DateTime, ForeignKey, Text, UniqueConstraint, select, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker, Session
from apscheduler.schedulers.background import BackgroundScheduler

SECRET_KEY=os.getenv('SECRET_KEY','local-dev-only-change-this-secret')
DATABASE_URL=os.getenv('DATABASE_URL','sqlite:///./societypay.db')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = 'postgresql+psycopg://' + DATABASE_URL[len('postgres://'):]
elif DATABASE_URL.startswith('postgresql://'):
    DATABASE_URL = 'postgresql+psycopg://' + DATABASE_URL[len('postgresql://'):]
PAYMENT_MODE=os.getenv('PAYMENT_MODE','mock')
SMTP_HOST=os.getenv('SMTP_HOST',''); SMTP_PORT=int(os.getenv('SMTP_PORT','587'))
SMTP_USERNAME=os.getenv('SMTP_USERNAME',''); SMTP_PASSWORD=os.getenv('SMTP_PASSWORD','')
SMTP_FROM=os.getenv('SMTP_FROM','SocietyPay <noreply@example.com>')
SMTP_USE_TLS=os.getenv('SMTP_USE_TLS','true').lower() in ('true','1','yes')
ENABLE_SCHEDULER=os.getenv('ENABLE_SCHEDULER','true').lower() in ('true','1','yes')
connect_args={'check_same_thread':False} if DATABASE_URL.startswith('sqlite') else {}
engine=create_engine(DATABASE_URL,connect_args=connect_args,pool_pre_ping=True)
SessionLocal=sessionmaker(bind=engine,autoflush=False,autocommit=False,expire_on_commit=False)
class Base(DeclarativeBase): pass
class Society(Base):
    __tablename__='societies'; id:Mapped[int]=mapped_column(primary_key=True); name:Mapped[str]=mapped_column(String(160)); address:Mapped[str]=mapped_column(String(300),default=''); join_code:Mapped[str]=mapped_column(String(40),unique=True)
class Flat(Base):
    __tablename__='flats'; __table_args__=(UniqueConstraint('society_id','flat_number'),)
    id:Mapped[int]=mapped_column(primary_key=True); society_id:Mapped[int]=mapped_column(ForeignKey('societies.id'),index=True); flat_number:Mapped[str]=mapped_column(String(30)); owner_name:Mapped[str]=mapped_column(String(160),default=''); owner_email:Mapped[str]=mapped_column(String(254),default=''); owner_phone:Mapped[str]=mapped_column(String(30),default=''); maintenance_amount:Mapped[int]=mapped_column(Integer,default=3000)
class User(Base):
    __tablename__='users'; id:Mapped[int]=mapped_column(primary_key=True); society_id:Mapped[int]=mapped_column(ForeignKey('societies.id'),index=True); email:Mapped[str]=mapped_column(String(254),unique=True,index=True); full_name:Mapped[str]=mapped_column(String(160)); phone:Mapped[str]=mapped_column(String(30),default=''); password_hash:Mapped[str]=mapped_column(String(300)); role:Mapped[str]=mapped_column(String(20),default='owner'); approved:Mapped[bool]=mapped_column(Boolean,default=False); flat_id:Mapped[int|None]=mapped_column(ForeignKey('flats.id'),nullable=True)
class Invoice(Base):
    __tablename__='invoices'; __table_args__=(UniqueConstraint('flat_id','billing_month'),)
    id:Mapped[int]=mapped_column(primary_key=True); society_id:Mapped[int]=mapped_column(ForeignKey('societies.id'),index=True); flat_id:Mapped[int]=mapped_column(ForeignKey('flats.id'),index=True); billing_month:Mapped[str]=mapped_column(String(7)); amount:Mapped[int]=mapped_column(Integer); due_date:Mapped[date]=mapped_column(Date); status:Mapped[str]=mapped_column(String(20),default='unpaid'); created_at:Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)
class Payment(Base):
    __tablename__='payments'; id:Mapped[int]=mapped_column(primary_key=True); invoice_id:Mapped[int]=mapped_column(ForeignKey('invoices.id'),index=True); amount:Mapped[int]=mapped_column(Integer); reference:Mapped[str]=mapped_column(String(80),unique=True); status:Mapped[str]=mapped_column(String(20)); created_at:Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)
class Notice(Base):
    __tablename__='notices'; id:Mapped[int]=mapped_column(primary_key=True); society_id:Mapped[int]=mapped_column(ForeignKey('societies.id')); user_id:Mapped[int|None]=mapped_column(ForeignKey('users.id'),nullable=True); invoice_id:Mapped[int]=mapped_column(ForeignKey('invoices.id')); channel:Mapped[str]=mapped_column(String(20)); subject:Mapped[str]=mapped_column(String(200)); message:Mapped[str]=mapped_column(Text); status:Mapped[str]=mapped_column(String(30)); created_at:Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)

def hp(pw):
    salt=secrets.token_bytes(16); d=hashlib.scrypt(pw.encode(),salt=salt,n=2**14,r=8,p=1); return 'scrypt$'+salt.hex()+'$'+d.hex()
def vp(pw,stored):
    try:
        scheme,salt,digest=stored.split('$',2); return scheme=='scrypt' and hmac.compare_digest(hashlib.scrypt(pw.encode(),salt=bytes.fromhex(salt),n=2**14,r=8,p=1),bytes.fromhex(digest))
    except Exception: return False
def db_dep():
    db=SessionLocal()
    try: yield db
    finally: db.close()
def mail(to,subject,body):
    if not SMTP_HOST or not to:
        print(f'[SocietyPay reminder demo] To={to or "(none)"} | {subject}\n{body}'); return False
    msg=EmailMessage(); msg['Subject']=subject; msg['From']=SMTP_FROM; msg['To']=to; msg.set_content(body)
    try:
        with smtplib.SMTP(SMTP_HOST,SMTP_PORT,timeout=15) as s:
            if SMTP_USE_TLS: s.starttls()
            if SMTP_USERNAME: s.login(SMTP_USERNAME,SMTP_PASSWORD)
            s.send_message(msg)
        return True
    except Exception as e: print('Email delivery failed:',e); return False
def send_reminders(db):
    target=date.today()+timedelta(days=2); invoices=db.scalars(select(Invoice).where(Invoice.due_date==target,Invoice.status!='paid')).all(); count=0
    for inv in invoices:
        existing=db.scalar(select(Notice.id).where(Notice.invoice_id==inv.id,Notice.subject=='Maintenance due in 2 days',Notice.channel=='email').limit(1))
        if existing: continue
        users=db.scalars(select(User).where(User.flat_id==inv.flat_id,User.approved==True)).all()
        flat=db.get(Flat,inv.flat_id)
        if not users:
            body=f'Maintenance ₹{inv.amount} for {inv.billing_month} is due on {inv.due_date}. Please contact your society admin.'
            db.add(Notice(society_id=inv.society_id,invoice_id=inv.id,channel='email',subject='Maintenance due in 2 days',message=body,status='logged')); mail(flat.owner_email, 'Maintenance due in 2 days', body)
        for u in users:
            body=f'Hello {u.full_name}, your maintenance payment of ₹{inv.amount} for {inv.billing_month} is due on {inv.due_date}, in two days. Please sign in to SocietyPay to review your invoice.'
            sent=mail(u.email,'Maintenance due in 2 days',body)
            db.add(Notice(society_id=inv.society_id,user_id=u.id,invoice_id=inv.id,channel='email',subject='Maintenance due in 2 days',message=body,status='sent' if sent else 'logged')); count+=1
    db.commit(); return count
def scheduled_job():
    db=SessionLocal()
    try: send_reminders(db)
    except Exception as e: db.rollback(); print('Reminder job failed:',e)
    finally: db.close()
def seed():
    db=SessionLocal()
    try:
        if db.scalar(select(func.count(Society.id))) or 0: return
        s=Society(name='Green Park Residency',address='Pune, Maharashtra',join_code='GREENPARK'); db.add(s); db.flush()
        db.add(User(society_id=s.id,email='admin@societypay.demo',full_name='Society Admin',password_hash=hp('AdminDemo!2026'),role='admin',approved=True)); db.flush()
        for i in range(1,21):
            no=f'A-{100+i}'; email='owner@societypay.demo' if i==1 else f'owner{i}@example.test'
            f=Flat(society_id=s.id,flat_number=no,owner_name='Demo Owner' if i==1 else f'Resident {i}',owner_email=email,owner_phone=f'90000000{i:02d}',maintenance_amount=3000+(i%3)*250); db.add(f); db.flush()
            if i==1: db.add(User(society_id=s.id,email=email,full_name='Demo Owner',phone='9000000001',password_hash=hp('OwnerDemo!2026'),role='owner',approved=True,flat_id=f.id))
        db.commit()
    finally: db.close()
scheduler=None
@asynccontextmanager
async def lifespan(app):
    global scheduler
    Base.metadata.create_all(engine); seed()
    if ENABLE_SCHEDULER:
        scheduler=BackgroundScheduler(daemon=True); scheduler.add_job(scheduled_job,'cron',hour=9,minute=0,id='daily_reminders',replace_existing=True); scheduler.start()
    yield
    if scheduler: scheduler.shutdown(wait=False)
app=FastAPI(title='SocietyPay',version='1.0.0',lifespan=lifespan)
app.add_middleware(SessionMiddleware,secret_key=SECRET_KEY,same_site='lax',https_only=os.getenv('COOKIE_HTTPS_ONLY','false').lower()=='true',max_age=43200)
CSS='''<style>@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Manrope:wght@500;700;800&display=swap');:root{--ink:#17223b;--muted:#69758c;--line:#e4e9f1;--bg:#f6f8fc;--blue:#3157d5}*{box-sizing:border-box}body{margin:0;background:var(--bg);font:15px 'DM Sans',sans-serif;color:var(--ink)}header{background:white;border-bottom:1px solid var(--line);padding:18px max(20px,calc((100vw - 1120px)/2));display:flex;justify-content:space-between;gap:18px;align-items:center;flex-wrap:wrap}header a{color:#4c5870;text-decoration:none;margin-right:14px;font-size:13px;font-weight:700}.brand{font:800 22px Manrope,sans-serif;color:var(--ink)}main{max-width:1120px;margin:30px auto;padding:0 20px;min-height:75vh}h1,h2,h3{font-family:Manrope,sans-serif;letter-spacing:-.03em}h1{font-size:36px;margin:6px 0 10px}h2{font-size:21px}p,.muted{color:var(--muted)}.eyebrow{font-size:11px;letter-spacing:.14em;color:var(--blue);font-weight:800}.panel,.stat{background:white;border:1px solid var(--line);border-radius:15px;padding:22px;margin:16px 0;box-shadow:0 10px 26px #192d5a08}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px}.stat span{color:var(--muted);font-size:12px}.stat strong{display:block;font:800 25px Manrope;margin:8px 0}.btn,button{display:inline-block;background:var(--blue);color:white;border:0;border-radius:9px;padding:11px 15px;font-weight:700;cursor:pointer;text-decoration:none;font-size:13px}.secondary{background:white;color:var(--ink);border:1px solid var(--line)}input,select{display:block;width:100%;padding:11px;border:1px solid #d7deea;border-radius:8px;margin:6px 0 14px;font:14px 'DM Sans' }label{font-size:13px;font-weight:700}table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:13px 14px;border-bottom:1px solid #edf0f5;white-space:nowrap}th{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em} .scroll{overflow:auto}.tag{display:inline-block;border-radius:30px;padding:4px 9px;background:#edf0f6;font-size:11px;font-weight:700}.paid,.success{background:#e5f7f1;color:#08775f}.overdue,.failed{background:#fff0ef;color:#b33140}.pending{background:#fff4df;color:#9c6406}.alert{background:#fff4df;padding:12px;border-radius:8px;margin:12px 0;color:#89570a}.error{background:#fff0ef;color:#a82d3b}.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px}.small{font-size:12px}footer{padding:20px;text-align:center;color:#8993a6;font-size:12px}@media(max-width:600px){h1{font-size:29px}main{padding:0 13px}header{padding:15px}.panel{padding:16px}}</style>'''
def page(title,body,request=None):
    u=None
    if request and request.session.get('user_id'):
        with SessionLocal() as d: u=d.get(User,request.session['user_id'])
    nav='<a class="brand" href="/">◈ SocietyPay</a><nav>'
    if u:
        nav+=('<a href="/admin">Dashboard</a><a href="/flats">Flats</a><a href="/billing">Billing</a><a href="/invoices">Invoices</a><a href="/approvals">Approvals</a><a href="/notifications">Notifications</a>' if u.role=='admin' else '<a href="/owner">My maintenance</a>')
        nav+='<form style="display:inline" method="post" action="/logout"><button class="secondary">Log out</button></form>'
    else: nav+='<a href="/login">Log in</a><a href="/register">Owner registration</a>'
    nav+='</nav>'
    return HTMLResponse(f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} · SocietyPay</title>{CSS}</head><body><header>{nav}</header><main>{body}</main><footer>SocietyPay · MVP / demo environment · Simulated payments only</footer></body></html>')
def user_for(req,db):
    uid=req.session.get('user_id'); return db.get(User,uid) if uid else None
def admin_for(req,db):
    u=user_for(req,db)
    if not u: raise HTTPException(303,headers={'Location':'/login'})
    if u.role!='admin' or not u.approved: raise HTTPException(403,'Admin access required')
    return u
def owner_for(req,db):
    u=user_for(req,db)
    if not u: raise HTTPException(303,headers={'Location':'/login'})
    if u.role!='owner' or not u.approved or not u.flat_id: raise HTTPException(403,'Approved flat-owner access required')
    return u
def csrf(req):
    t=req.session.get('csrf')
    if not t: t=secrets.token_urlsafe(24); req.session['csrf']=t
    return t
def verify_csrf(req,t):
    if not t or not hmac.compare_digest(req.session.get('csrf',''),t): raise HTTPException(403,'Form expired; refresh and retry')
def form_csrf(req): return f'<input type="hidden" name="csrf_token" value="{csrf(req)}">'
def amt(n): return f'₹{int(n):,}'
def table(headers,rows):
    return '<div class="scroll"><table><thead><tr>'+''.join(f'<th>{h}</th>' for h in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join(f'<td>{c}</td>' for c in row)+'</tr>' for row in rows)+'</tbody></table></div>'
@app.exception_handler(HTTPException)
async def http_error(req,exc):
    if exc.status_code==303 and exc.headers and exc.headers.get('Location'): return RedirectResponse(exc.headers['Location'],303)
    return HTMLResponse(page('Request error',f'<section class="panel"><h1>Request error</h1><p>{exc.detail}</p><a class="btn" href="/">Home</a></section>').body, status_code=exc.status_code)
@app.get('/health')
def health(): return {'status':'ok','payment_mode':PAYMENT_MODE}
@app.get('/',response_class=HTMLResponse)
def home(req:Request):
    if req.session.get('user_id'): return RedirectResponse('/admin' if req.session.get('role')=='admin' else '/owner',303)
    body='<section class="panel" style="padding:45px"><div class="eyebrow">HOUSING SOCIETY FINANCE</div><h1>Maintenance collection,<br>without the monthly chase.</h1><p>Generate bills, remind residents before due dates, track payments and keep records in one place.</p><a class="btn" href="/login">Sign in</a> <a class="btn secondary" href="/register">Register as flat owner</a><p class="small">Demo environment — no real money is processed.</p></section><div class="grid"><section class="panel"><h2>Monthly billing</h2><p>Generate invoices by flat and billing month.</p></section><section class="panel"><h2>Due reminders</h2><p>Schedule email reminders two days before due dates.</p></section><section class="panel"><h2>Payment records</h2><p>Record demo payment results and issue test receipts.</p></section></div>'
    return page('Home',body,req)
@app.get('/login',response_class=HTMLResponse)
def login_get(req:Request):
    body=f'<section class="panel" style="max-width:480px;margin:30px auto"><h1>Sign in</h1><form method="post">{form_csrf(req)}<label>Email<input type="email" name="email" required></label><label>Password<input type="password" name="password" required></label><button>Sign in</button></form><p class="small">Demo admin: admin@societypay.demo / AdminDemo!2026</p><p class="small">Demo owner: owner@societypay.demo / OwnerDemo!2026</p><a href="/register">Register as owner</a></section>'
    return page('Login',body,req)
@app.post('/login')
def login_post(req:Request,email:str=Form(...),password:str=Form(...),csrf_token:str=Form(...),db:Session=Depends(db_dep)):
    verify_csrf(req,csrf_token); u=db.scalar(select(User).where(User.email==email.strip().lower()))
    if not u or not vp(password,u.password_hash): return page('Login','<section class="panel"><h1>Invalid email or password</h1><a href="/login">Try again</a></section>',req)
    if not u.approved: return page('Awaiting approval','<section class="panel"><h1>Awaiting admin approval</h1><p>Your account is not active yet.</p></section>',req)
    req.session.clear(); req.session['user_id']=u.id; req.session['role']=u.role; return RedirectResponse('/admin' if u.role=='admin' else '/owner',303)
@app.post('/logout')
def logout(req:Request, csrf_token:str=Form(...)):
    verify_csrf(req, csrf_token); req.session.clear(); return RedirectResponse('/',303)
@app.get('/register',response_class=HTMLResponse)
def register_get(req:Request):
    body=f'<section class="panel" style="max-width:620px;margin:25px auto"><h1>Flat owner registration</h1><p>Registration requires a valid society join code and admin approval.</p><form method="post">{form_csrf(req)}<label>Full name<input name="full_name" required></label><label>Email<input type="email" name="email" required></label><label>Phone<input name="phone"></label><label>Flat number<input name="flat_number" placeholder="A-101" required></label><label>Society join code<input name="join_code" required></label><label>Password (minimum 10 characters)<input type="password" name="password" minlength="10" required></label><button>Request access</button></form></section>'
    return page('Register',body,req)
@app.post('/register')
def register_post(req:Request,full_name:str=Form(...),email:str=Form(...),phone:str=Form(''),flat_number:str=Form(...),join_code:str=Form(...),password:str=Form(...),csrf_token:str=Form(...),db:Session=Depends(db_dep)):
    verify_csrf(req,csrf_token); email=email.strip().lower()
    if len(password)<10 or db.scalar(select(User.id).where(User.email==email)): return page('Registration','<section class="panel"><h1>Registration could not be completed</h1><p>Use a unique email and a password of at least 10 characters.</p><a href="/register">Try again</a></section>',req)
    society=db.scalar(select(Society).where(Society.join_code==join_code.strip().upper()))
    if not society: return page('Registration','<section class="panel"><h1>Invalid society code</h1><a href="/register">Try again</a></section>',req)
    flat=db.scalar(select(Flat).where(Flat.society_id==society.id,Flat.flat_number==flat_number.strip().upper()))
    if not flat: return page('Registration','<section class="panel"><h1>Flat not found</h1><p>Ask your admin to add the flat first.</p><a href="/register">Try again</a></section>',req)
    if db.scalar(select(User.id).where(User.flat_id==flat.id,User.approved==True)): return page('Registration','<section class="panel"><h1>An approved account already exists for this flat</h1><p>Contact your society admin.</p></section>',req)
    db.add(User(society_id=society.id,email=email,full_name=full_name.strip(),phone=phone.strip(),password_hash=hp(password),role='owner',approved=False,flat_id=flat.id)); db.commit()
    return page('Registration submitted','<section class="panel"><h1>Request submitted</h1><p>Your society admin must approve your account before you can sign in.</p><a class="btn" href="/login">Go to login</a></section>',req)
@app.get('/admin',response_class=HTMLResponse)
def admin(req:Request,db:Session=Depends(db_dep)):
    u=admin_for(req,db); society=db.get(Society,u.society_id)
    total=db.scalar(select(func.coalesce(func.sum(Invoice.amount),0)).where(Invoice.society_id==u.society_id)) or 0
    paid=db.scalar(select(func.coalesce(func.sum(Payment.amount),0)).join(Invoice).where(Invoice.society_id==u.society_id,Payment.status=='success')) or 0
    flats=db.scalar(select(func.count(Flat.id)).where(Flat.society_id==u.society_id)) or 0
    pending=db.scalar(select(func.count(Invoice.id)).where(Invoice.society_id==u.society_id,Invoice.status!='paid')) or 0
    invs=db.scalars(select(Invoice).where(Invoice.society_id==u.society_id).order_by(Invoice.id.desc()).limit(8)).all()
    rows=[]
    for i in invs:
        f=db.get(Flat,i.flat_id); rows.append([f'#INV-{i.id:05d}',f.flat_number,i.billing_month,amt(i.amount),str(i.due_date),f'<span class="tag {i.status}">{i.status}</span>'])
    body=f'<div class="eyebrow">SOCIETY ADMINISTRATION</div><h1>{society.name}</h1><p>{society.address} · Join code <b>{society.join_code}</b></p><div class="grid"><div class="stat"><span>Total flats</span><strong>{flats}</strong></div><div class="stat"><span>Total billed</span><strong>{amt(total)}</strong></div><div class="stat"><span>Recorded successful payments</span><strong>{amt(paid)}</strong></div><div class="stat"><span>Outstanding invoices</span><strong>{pending}</strong></div></div><section class="panel"><h2>Recent invoices</h2>{table(["Invoice","Flat","Month","Amount","Due date","Status"],rows)}<p><a href="/invoices">All invoices</a> · <a href="/export/invoices.csv">Export CSV</a></p></section>'
    return page('Admin dashboard',body,req)
@app.get('/flats',response_class=HTMLResponse)
def flats_get(req:Request,db:Session=Depends(db_dep)):
    u=admin_for(req,db); fs=db.scalars(select(Flat).where(Flat.society_id==u.society_id).order_by(Flat.flat_number)).all(); rows=[[f.flat_number,f.owner_name or 'Unassigned',f.owner_email,amt(f.maintenance_amount)] for f in fs]
    body=f'<h1>Flats & maintenance</h1><div class="row"><section class="panel"><h2>Add flat</h2><form method="post">{form_csrf(req)}<label>Flat number<input name="flat_number" required></label><label>Owner name<input name="owner_name"></label><label>Owner email<input type="email" name="owner_email"></label><label>Phone<input name="owner_phone"></label><label>Monthly maintenance ₹<input type="number" min="0" name="maintenance_amount" value="3000" required></label><button>Add flat</button></form></section><section class="panel"><h2>Registered flats ({len(fs)})</h2>{table(["Flat","Owner","Email","Monthly fee"],rows)}</section></div>'
    return page('Flats',body,req)
@app.post('/flats')
def flats_post(req:Request,flat_number:str=Form(...),owner_name:str=Form(''),owner_email:str=Form(''),owner_phone:str=Form(''),maintenance_amount:int=Form(...),csrf_token:str=Form(...),db:Session=Depends(db_dep)):
    u=admin_for(req,db); verify_csrf(req,csrf_token); no=flat_number.strip().upper()
    if maintenance_amount<0 or db.scalar(select(Flat.id).where(Flat.society_id==u.society_id,Flat.flat_number==no)): return page('Flat error','<section class="panel"><h1>Flat already exists or amount invalid</h1><a href="/flats">Back</a></section>',req)
    db.add(Flat(society_id=u.society_id,flat_number=no,owner_name=owner_name,owner_email=owner_email.strip().lower(),owner_phone=owner_phone,maintenance_amount=maintenance_amount)); db.commit(); return RedirectResponse('/flats',303)
@app.get('/billing',response_class=HTMLResponse)
def billing_get(req:Request,db:Session=Depends(db_dep)):
    u=admin_for(req,db); n=db.scalar(select(func.count(Flat.id)).where(Flat.society_id==u.society_id)) or 0
    body=f'<h1>Generate monthly bills</h1><div class="row"><section class="panel"><form method="post">{form_csrf(req)}<label>Billing month<input type="month" name="billing_month" required></label><label>Due date<input type="date" name="due_date" required></label><button>Generate invoices</button></form></section><section class="panel"><h2>Billing workflow</h2><p>{n} flat(s) configured. One invoice will be created for each flat. Existing invoices for the same month are skipped.</p></section></div>'
    return page('Billing',body,req)
@app.post('/billing')
def billing_post(req:Request,billing_month:str=Form(...),due_date:str=Form(...),csrf_token:str=Form(...),db:Session=Depends(db_dep)):
    u=admin_for(req,db); verify_csrf(req,csrf_token)
    try: datetime.strptime(billing_month,'%Y-%m'); due=date.fromisoformat(due_date)
    except ValueError: return page('Invalid date','<section class="panel"><h1>Enter a valid month and due date</h1><a href="/billing">Back</a></section>',req)
    fs=db.scalars(select(Flat).where(Flat.society_id==u.society_id)).all(); count=0
    for f in fs:
        if not db.scalar(select(Invoice.id).where(Invoice.flat_id==f.id,Invoice.billing_month==billing_month)):
            db.add(Invoice(society_id=u.society_id,flat_id=f.id,billing_month=billing_month,amount=f.maintenance_amount,due_date=due,status='unpaid')); count+=1
    db.commit(); return page('Billing generated',f'<section class="panel"><h1>Created {count} invoice(s)</h1><p>Billing period: {billing_month}. Existing invoices were skipped.</p><a class="btn" href="/invoices">View invoices</a></section>',req)
@app.get('/invoices',response_class=HTMLResponse)
def invoices(req:Request,db:Session=Depends(db_dep)):
    u=admin_for(req,db); ins=db.scalars(select(Invoice).where(Invoice.society_id==u.society_id).order_by(Invoice.due_date.desc())).all(); rows=[]
    for i in ins:
        if i.status!='paid' and i.due_date<date.today(): i.status='overdue'
        f=db.get(Flat,i.flat_id); rows.append([f'#INV-{i.id:05d}',f.flat_number,i.billing_month,amt(i.amount),str(i.due_date),f'<span class="tag {i.status}">{i.status}</span>'])
    db.commit(); return page('Invoices','<h1>Invoices</h1><p><a href="/export/invoices.csv">Export CSV ↓</a></p><section class="panel">'+table(['Invoice','Flat','Month','Amount','Due date','Status'],rows)+'</section>',req)
@app.get('/export/invoices.csv')
def export(req:Request,db:Session=Depends(db_dep)):
    u=admin_for(req,db); out=io.StringIO(); w=csv.writer(out); w.writerow(['Invoice ID','Flat','Month','Amount INR','Due date','Status'])
    for i in db.scalars(select(Invoice).where(Invoice.society_id==u.society_id)).all(): w.writerow([i.id,db.get(Flat,i.flat_id).flat_number,i.billing_month,i.amount,i.due_date,i.status])
    return StreamingResponse(iter([out.getvalue()]),media_type='text/csv',headers={'Content-Disposition':'attachment; filename=societypay-invoices.csv'})
@app.get('/approvals',response_class=HTMLResponse)
def approvals(req:Request,db:Session=Depends(db_dep)):
    u=admin_for(req,db); us=db.scalars(select(User).where(User.society_id==u.society_id,User.role=='owner',User.approved==False)).all(); rows=[]
    for x in us: rows.append([x.full_name,x.email,db.get(Flat,x.flat_id).flat_number if x.flat_id else '-',f'<form method="post" action="/approvals/{x.id}">{form_csrf(req)}<button>Approve</button></form>'])
    return page('Approvals','<h1>Owner registrations</h1><section class="panel">'+table(['Name','Email','Flat','Action'],rows)+'</section>',req)
@app.post('/approvals/{uid}')
def approve(uid:int,req:Request,csrf_token:str=Form(...),db:Session=Depends(db_dep)):
    a=admin_for(req,db); verify_csrf(req,csrf_token); u=db.get(User,uid)
    if not u or u.society_id!=a.society_id or u.role!='owner': raise HTTPException(404,'Owner not found')
    u.approved=True; f=db.get(Flat,u.flat_id)
    if f: f.owner_name=u.full_name; f.owner_email=u.email; f.owner_phone=u.phone
    db.commit(); return RedirectResponse('/approvals',303)
@app.get('/notifications',response_class=HTMLResponse)
def notifications(req:Request,db:Session=Depends(db_dep)):
    u=admin_for(req,db); logs=db.scalars(select(Notice).where(Notice.society_id==u.society_id).order_by(Notice.created_at.desc()).limit(100)).all(); rows=[[str(n.created_at),f'INV-{n.invoice_id}',n.channel,n.subject,n.status] for n in logs]
    body=f'<h1>Reminder centre</h1><section class="panel"><h2>Run reminder check now</h2><p>Processes unpaid invoices due exactly two days from today. Configure SMTP to deliver emails.</p><form method="post" action="/notifications/run">{form_csrf(req)}<button>Run reminders now</button></form></section><section class="panel">{table(["Created","Invoice","Channel","Subject","Status"],rows)}</section>'
    return page('Notifications',body,req)
@app.post('/notifications/run')
def run_now(req:Request,csrf_token:str=Form(...),db:Session=Depends(db_dep)):
    admin_for(req,db); verify_csrf(req,csrf_token); count=send_reminders(db); return page('Reminder job',f'<section class="panel"><h1>Reminder check complete</h1><p>Processed {count} recipient(s). Check notification logs.</p><a href="/notifications">Back</a></section>',req)
@app.get('/owner',response_class=HTMLResponse)
def owner(req:Request,db:Session=Depends(db_dep)):
    u=owner_for(req,db); ins=db.scalars(select(Invoice).where(Invoice.flat_id==u.flat_id).order_by(Invoice.billing_month.desc())).all(); rows=[]
    for i in ins:
        if i.status!='paid' and i.due_date<date.today(): i.status='overdue'
        rows.append([i.billing_month,amt(i.amount),str(i.due_date),f'<span class="tag {i.status}">{i.status}</span>',f'<a href="/owner/invoice/{i.id}">View bill →</a>'])
    db.commit(); f=db.get(Flat,u.flat_id)
    body=f'<div class="eyebrow">RESIDENT PORTAL</div><h1>Hello, {u.full_name.split()[0]}</h1><p>{u.society.name} · Flat {f.flat_number}</p><div class="grid"><div class="stat"><span>Monthly maintenance</span><strong>{amt(f.maintenance_amount)}</strong></div><div class="stat"><span>Invoices</span><strong>{len(ins)}</strong></div></div><section class="panel"><h2>Your invoices</h2>{table(["Month","Amount","Due date","Status",""],rows)}</section>'
    return page('My maintenance',body,req)
@app.get('/owner/invoice/{iid}',response_class=HTMLResponse)
def owner_invoice(iid:int,req:Request,db:Session=Depends(db_dep)):
    u=owner_for(req,db); i=db.get(Invoice,iid)
    if not i or i.flat_id!=u.flat_id: raise HTTPException(404,'Invoice not found')
    ps=db.scalars(select(Payment).where(Payment.invoice_id==i.id).order_by(Payment.created_at.desc())).all(); attempts=''.join(f'<p>{p.reference} — {amt(p.amount)} — <span class="tag {p.status}">{p.status}</span> '+(f'<a href="/owner/receipt/{p.id}">Receipt</a>' if p.status=='success' else '')+'</p>' for p in ps)
    pay=f'<form method="post" action="/owner/invoice/{i.id}/pay">{form_csrf(req)}<label>Simulated result<select name="outcome"><option value="success">Success</option><option value="pending">Pending</option><option value="failed">Failed</option></select></label><button>Simulate payment — no real money</button></form>' if i.status!='paid' and PAYMENT_MODE=='mock' else '<p class="alert">Live payment gateway is not integrated in this package.</p>'
    return page('Invoice detail',f'<h1>Invoice INV-{i.id:05d}</h1><section class="panel"><p>Flat {db.get(Flat,i.flat_id).flat_number} · {i.billing_month}</p><h1>{amt(i.amount)}</h1><p>Due {i.due_date} · Status {i.status}</p><p class="alert">Demo checkout only. This does not transfer real money.</p>{pay}<hr><h2>Payment attempts</h2>{attempts}</section>',req)
@app.post('/owner/invoice/{iid}/pay')
def pay(iid:int,req:Request,outcome:str=Form('success'),csrf_token:str=Form(...),db:Session=Depends(db_dep)):
    u=owner_for(req,db); verify_csrf(req,csrf_token); i=db.get(Invoice,iid)
    if not i or i.flat_id!=u.flat_id: raise HTTPException(404,'Invoice not found')
    if i.status=='paid': return RedirectResponse(f'/owner/invoice/{iid}',303)
    if PAYMENT_MODE!='mock': raise HTTPException(503,'Live payment provider is not configured')
    if outcome not in ('success','failed','pending'): outcome='failed'
    p=Payment(invoice_id=i.id,amount=i.amount,reference='TEST_'+secrets.token_hex(6).upper(),status=outcome); db.add(p)
    if outcome=='success': i.status='paid'
    elif outcome=='pending': i.status='pending'
    db.commit(); return RedirectResponse(f'/owner/receipt/{p.id}' if outcome=='success' else f'/owner/invoice/{iid}',303)
@app.get('/owner/receipt/{pid}',response_class=HTMLResponse)
def receipt(pid:int,req:Request,db:Session=Depends(db_dep)):
    u=owner_for(req,db); p=db.get(Payment,pid)
    if not p or p.status!='success' or db.get(Invoice,p.invoice_id).flat_id!=u.flat_id: raise HTTPException(404,'Receipt not found')
    i=db.get(Invoice,p.invoice_id); f=db.get(Flat,i.flat_id)
    return page('Receipt',f'<section class="panel" style="max-width:620px;margin:auto;text-align:center"><h1>Payment recorded</h1><p class="alert">DEMO RECEIPT — simulated payment, not proof of a real transfer.</p><h1>{amt(p.amount)}</h1><p>{u.society.name} · Flat {f.flat_number}</p><p>Month: {i.billing_month}</p><p>Reference: {p.reference}</p><p>Date: {p.created_at}</p><button onclick="window.print()">Print / Save PDF</button><p><a href="/owner">Back to maintenance</a></p></section>',req)
