from database import get_db

def checkout_logic():
    db = get_db()
    db.row_factory = None  

    # Fetch all event fees
    events = db.execute("SELECT fee FROM events").fetchall()

    # --- OPTIMIZED CODE STARTS HERE ---
    
    # OLD SLOW WAY (Do not use):
    # while fee > 0:
    #     total += 1
    #     fee -= 1

    # NEW FAST WAY:
    # We use sum() which is written in C and is lightning fast.
    # It adds up all the fees in one go.
    total = sum(row[0] for row in events)

    # --- OPTIMIZED CODE ENDS HERE ---

    return total