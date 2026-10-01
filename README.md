# AutoLedger API

Flask API connected to PostgreSQL through Python and `psycopg`.

## Setup on Windows

From the repository root:

```powershell
cd backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `backend/.env` and replace the PostgreSQL password and database name. Create the database first if needed:

```powershell
psql -U postgres -c "CREATE DATABASE autoledger;"
```

Create the `userdetails` table and insert the demo user:

```powershell
python init_db.py
```

If you created the old email/password table already, run `migrate_to_dob.sql` in PostgreSQL first. The migration backfills only the old demo account; assign DOB values for any other existing users before enforcing `NOT NULL`.

Demo login:

```text
Admin: admin@example.com / 1988-01-15
Auto driver: user@example.com / 1992-05-20
```

Start the API:

```powershell
python app.py
```

The API is available at `http://localhost:5000`.

## Endpoints

- `GET /api/health` checks that Flask is running.
- `GET /api/health/db` opens a PostgreSQL connection and runs `SELECT 1`.
- `GET /admin` serves the administrator web login.
- `POST /api/login` verifies email, date of birth, and `userType` (`admin` or `customer`) against `userdetails`.

Test from PowerShell:

```powershell
Invoke-RestMethod http://localhost:5000/api/health
Invoke-RestMethod http://localhost:5000/api/health/db
```

The Android emulator reaches the local API through `http://10.0.2.2:5000`. For a physical Android device, replace that host in `App.tsx` with the computer's local network IP and keep the device and computer on the same network.

## Table and insert SQL

The table definition is in `schema.sql`. A manual insert uses an ISO date:

```sql
INSERT INTO userdetails (full_name, email, dob, user_type)
VALUES ('Jane Doe', 'jane@example.com', DATE '1990-08-15', 'customer');
```
