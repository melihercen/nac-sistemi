from passlib.context import CryptContext
import psycopg2
import os

pwd_context=CryptContext(schemes=["bcrypt"],deprecated="auto")
hashed_password=pwd_context.hash("123456")


conn=psycopg2.connect(
    dbname=os.getenv("POSTGRES_DB","nac_db"),
    user=os.getenv("POSTGRES_USER","nac_user"),
    password=os.getenv("POSTGRES_PASSWORD","super_secret_db_pass"),
    host="postgres"
)

cursor=conn.cursor()

try:
    cursor.execute("TRUNCATE TABLE radcheck,radusergroup,radgroupreply CASCADE;")

    cursor.execute(
        "INSERT INTO radcheck (username,attribute,op,value) VALUES (%s,'Cleartext-Password',':=',%s)",
        ('admin_user',hashed_password)
    )

    cursor.execute(
        "INSERT INTO radusergroup (username,groupname,priority) VALUES (%s,%s,%s)",
        ('admin_user','admin_group',1)
    )

    cursor.execute(
        "INSERT INTO radgroupreply (groupname,attribute,op,value) VALUES (%s,%s,%s,%s)",
        ('admin_group','Tunnel-Private-Group-Id','=','10')
    )

    cursor.execute(
    "INSERT INTO mac_addresses (mac_address, groupname, description) VALUES (%s, %s, %s)",
    ('AA:BB:CC:DD:EE:FF', 'employee_group', 'Test yazici')
)

# employee_group için VLAN politikası
    cursor.execute(
        "INSERT INTO radgroupreply (groupname, attribute, op, value) VALUES (%s, %s, %s, %s)",
        ('employee_group', 'Tunnel-Private-Group-Id', '=', '20')
    )

    # guest_group için VLAN politikası (bilinmeyen MAC'ler için)
    cursor.execute(
        "INSERT INTO radgroupreply (groupname, attribute, op, value) VALUES (%s, %s, %s, %s)",
        ('guest_group', 'Tunnel-Private-Group-Id', '=', '99')
    )

    print("MAB senaryosu için Yazıcı MAC Adresi (AA-BB-CC-DD-EE-FF) ve VLAN 20 başarıyla eklendi!")

    conn.commit()
    print("Test kullanıcısı (admin_user) ve VLAN politikaları başarıyla eklendi!")
except Exception as e:
    print(f"Hata oluştu: {e}")
finally:
    conn.close()