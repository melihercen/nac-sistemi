from fastapi import FastAPI, HTTPException, Request, Depends, status
import psycopg2
from psycopg2.extras import RealDictCursor
import redis
import os
from passlib.context import CryptContext



app=FastAPI(title="NAC Policy Engine")

pwd_context=CryptContext(schemes=["bcrypt"],deprecated="auto")

redis_client=redis.Redis(
    host=os.getenv("REDIS_HOST","redis"),
    port=int(os.getenv("REDIS_PORT",6379)),
    decode_responses=True
)


def get_db_connection():
    conn=psycopg2.connect(
        dbname=os.getenv("POSTGRES_DB"),
        user=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD"),
        host="postgres",
        cursor_factory=RealDictCursor
    )
    try:
        yield conn
    finally:
        conn.close()


@app.post("/auth")
async def authenticate(request: Request,db=Depends(get_db_connection)):
    data=await request.json()
    username=data.get("User-Name")
    password=data.get("User-Password")

    if not username:
        raise HTTPException(status_code=400,detail="User_Name eksik")
    
    rate_limit_key=f"rate_limit:{username}"
    attempts=redis_client.get(rate_limit_key)
    if attempts and int(attempts)>=5:
        return {"control:Auth-Type": "Reject"}
    cursor=db.cursor()
    cursor.execute("SELECT value FROM radcheck WHERE username= %s AND attribute='Cleartext-Password'",(username,))
    user_record=cursor.fetchone()

    if user_record:
        hashed_password=user_record['value']
        if pwd_context.verify(password,hashed_password):
            redis_client.delete(rate_limit_key)
            return {"control:Auth-Type":"Accept"}
    redis_client.incr(rate_limit_key)
    redis_client.expire(rate_limit_key,300)
    raise HTTPException(status_code=401, detail="Yanlis Sifre")

@app.post("/authorize")
async def authorize(request: Request, db=Depends(get_db_connection)):
    """VLAN, policy atribütleri dönme"""
    data = await request.json()
    username = data.get("User-Name")

    if not username:
         raise HTTPException(status_code=400, detail="User-Name eksik")

    cursor = db.cursor()
    
    # 1. Kullanıcının hangi gruba ait olduğunu bul
    # Eğer bir kullanıcı birden fazla gruptaysa, önceliği (priority) en yüksek olanı alıyoruz.
    cursor.execute(
        "SELECT groupname FROM radusergroup WHERE username = %s ORDER BY priority DESC LIMIT 1", 
        (username,)
    )
    group_record = cursor.fetchone()

    # FreeRADIUS'a döndürülecek yanıt sözlüğü
    response_data = {}

    if group_record:
        groupname = group_record['groupname']
        
        # 2. O gruba ait VLAN ve diğer reply atribütlerini çek
        cursor.execute(
            "SELECT attribute, value FROM radgroupreply WHERE groupname = %s", 
            (groupname,)
        )
        group_replies = cursor.fetchall()

        # 3. Veritabanından gelen verileri FreeRADIUS'un rlm_rest modülünün anlayacağı formata çevir
        for reply in group_replies:
            # FreeRADIUS'a bu atribütün bir "reply" (kullanıcıya geri gönderilecek) atribütü olduğunu söylüyoruz
            attr_name = f"reply:{reply['attribute']}"
            response_data[attr_name] = reply['value']

    # Eğer kullanıcının bir grubu varsa (örneğin Tunnel-Private-Group-Id: 10 gibi) bu veriler döner.
    # Grubu yoksa boş bir JSON döner ve cihaz switch/AP üzerindeki varsayılan ağa düşer.
    return response_data

@app.post("/accounting")
async def accounting(request: Request, db=Depends(get_db_connection)):
    data=await request.json()

    status_type=data.get("Acct-Status-Type")
    session_id=data.get("Acct-Session-Id")

    username=data.get("User-Name","unknown")
    nas_ip=data.get("NAS-IP-Address","0.0.0.0")
    input_octets=data.get("Acct-Input-Octets",0)
    output_octets=data.get("Acct-Output-Octets",0)
    session_time=data.get("Acct-Session-Time",0)
    terminate_cause=data.get("Acct-Terminate-Cause","")

    if not status_type or not session_id:
        return {"status":"ignored"}
    
    cursor=db.cursor()
    redis_session_key=f"session:{session_id}"

    if status_type=="Start":
        cursor.execute("""
                INSERT INTO radacct (acctsessionid,username,nasipaddress,acctstarttime)
                VALUES (%s,%s,%s,NOW())
                       """,(session_id,username,nas_ip))
        db.commit()

        session_data={
            "username":username,
            "nas_ip":nas_ip,
            "status":"active"
        }
        redis_client.hset(redis_session_key,mapping=session_data)

    elif status_type=="Interim-Update":
        cursor.execute("""
            UPDATE radacct
            SET acctupdatetime=NOW(),
                acctinputoctets= %s,
                acctoutputoctets= %s,
                acctsessiontime= %s
            WHERE acctsessionid= %s
                       """,(input_octets,output_octets,session_time,session_id))
        db.commit()
    
    elif status_type=="Stop":
        cursor.execute("""
            UPDATE radacct
            SET acctstoptime= NOW(),
                acctinputoctets= %s,
                acctoutputoctets= %s,
                acctsessiontime= %s,
                acctterminatecause= %s
            WHERE acctsessionid= %s
                       """,(input_octets,output_octets,session_time,terminate_cause,session_id))
        db.commit()

        redis_client.delete(redis_session_key)

    return {"status":"success"}

@app.get("/user")
async def get_users(db=Depends(get_db_connection)):
    cursor=db.cursor()
    cursor.execute("SELECT username FROM radcheck WHERE attribute = 'Cleartext-Password'")
    users=cursor.fetchall()
    return {"users":[user['username'] for user in users]}

@app.get("/sessions/active")
async def get_active_sessions():
    keys=redis_client.keys("session:*")
    active_sessions=[]

    for key in keys:
        session_info=redis_client.hgetall(key)
        session_info['session_id']=key.replace("session:","")
        active_sessions.append(session_info)

    return {"active_sessions":active_sessions,"count":len(active_sessions)}

@app.post("/mab")
async def mab(request: Request, db=Depends(get_db_connection)):
    try:
        data = await request.json()
    except:
        data = {}
    
    # FreeRADIUS MAC'i Calling-Station-Id olarak gönderiyor
    mac = data.get("Calling-Station-Id", "").upper()
    
    if not mac:
        return {"control:Auth-Type": "Reject"}
    
    cursor = db.cursor()
    cursor.execute(
        "SELECT groupname FROM mac_addresses WHERE mac_address = %s",
        (mac,)
    )
    mac_record = cursor.fetchone()
    
    response_data = {}
    
    if mac_record:
        # Bilinen MAC — grubuna göre VLAN ata
        groupname = mac_record['groupname']
        cursor.execute(
            "SELECT attribute, value FROM radgroupreply WHERE groupname = %s",
            (groupname,)
        )
        replies = cursor.fetchall()
        for reply in replies:
            response_data[f"reply:{reply['attribute']}"] = reply['value']
        response_data["control:Auth-Type"] = "Accept"
    else:
        # Bilinmeyen MAC — guest VLAN'a at
        cursor.execute(
            "SELECT attribute, value FROM radgroupreply WHERE groupname = 'guest_group'"
        )
        replies = cursor.fetchall()
        for reply in replies:
            response_data[f"reply:{reply['attribute']}"] = reply['value']
        response_data["control:Auth-Type"] = "Accept"
    
    return response_data
