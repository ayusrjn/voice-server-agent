import os
from datetime import datetime, timezone
from dotenv import load_dotenv
import motor.motor_asyncio

# Load .env from the parent directory to ensure MONGODB_URI is always available, even if run independently
dotenv_path = os.path.join(os.path.dirname(__file__), '..', '.env')
load_dotenv(dotenv_path)

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/test")
client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URI)
# Use the database specified in the URI, or default to "test" (which Mongoose uses by default)
db = client.get_default_database(default="test")

# ──────────────────────────────────────────────
# Refund policy constants
# ──────────────────────────────────────────────
REFUND_WINDOW_HOURS = 2  # Only allow refunds within 2 hours of delivery

# LTV (Lifetime Value) tiers — higher-value customers get better refund %
LTV_TIERS = [
    (10000, 1.00),   # ₹10,000+  → 100% refund
    (5000,  0.75),   # ₹5,000+   → 75% refund
    (2000,  0.50),   # ₹2,000+   → 50% refund
    (0,     0.30),   # Below      → 30% refund
]


def _get_refund_percentage(total_ltv: float) -> tuple[float, str]:
    """Determine refund percentage based on customer lifetime value."""
    for threshold, pct in LTV_TIERS:
        if total_ltv >= threshold:
            tier_label = f"₹{threshold:,}+" if threshold > 0 else "new customer"
            return pct, f"LTV tier: {tier_label} (total spend ₹{total_ltv:,.0f}) → {int(pct*100)}% refund"
    return 0.30, "default tier → 30% refund"


# ──────────────────────────────────────────────
# Tool 1: get_user_profile
# ──────────────────────────────────────────────
async def get_user_profile(user_id: int) -> str:
    """
    Fetches user profile details including name, wallet balance,
    and total lifetime spend across all orders (LTV).
    """
    try:
        user = await db.users.find_one({"_id": user_id})
        if not user:
            return "User not found."

        # Calculate total lifetime value (sum of all order amounts)
        pipeline = [
            {"$match": {"user_id": user_id}},
            {"$group": {"_id": None, "total_spend": {"$sum": "$total_amount"}, "order_count": {"$sum": 1}}}
        ]
        result = await db.orders.aggregate(pipeline).to_list(length=1)
        total_spend = result[0]["total_spend"] if result else 0
        order_count = result[0]["order_count"] if result else 0

        return (
            f"User Profile:\n"
            f"  Name: {user.get('name', 'N/A')}\n"
            f"  Phone: {user.get('phone', 'N/A')}\n"
            f"  Email: {user.get('email', 'N/A')}\n"
            f"  Wallet Balance: ₹{user.get('wallet_balance', 0):,.2f}\n"
            f"  Total Orders: {order_count}\n"
            f"  Lifetime Spend (LTV): ₹{total_spend:,.2f}\n"
            f"  Customer Tier: {'Loyal' if total_spend >= 10000 else 'Regular' if total_spend >= 5000 else 'Moderate' if total_spend >= 2000 else 'New'}"
        )
    except Exception as e:
        return f"Error fetching user profile: {e}"


# ──────────────────────────────────────────────
# Tool 2: check_order_status (enhanced)
# ──────────────────────────────────────────────
async def check_order_status(user_id: int) -> str:
    """
    Checks the active and historical order status for a given user.
    Returns order details including timestamps and time since delivery.
    """
    try:
        cursor = db.orders.find({"user_id": user_id}).sort("timestamp", -1)
        orders = await cursor.to_list(length=100)

        if not orders:
            return "The user has no past or active orders."

        now = datetime.now(timezone.utc)
        lines = []
        for o in orders:
            ts = o.get("timestamp")
            ts_str = ts.strftime("%d %b %Y, %I:%M %p") if ts else "Unknown"

            # Calculate how long ago the order was
            if ts and o.get("status") == "Delivered":
                delta = now - ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else now - ts
                hours_ago = delta.total_seconds() / 3600
                time_note = f" ({hours_ago:.1f} hours ago)"
                refund_eligible = "✅ Refund eligible" if hours_ago <= REFUND_WINDOW_HOURS else "❌ Past 2-hour refund window"
            else:
                time_note = ""
                refund_eligible = "N/A (not delivered)" if o.get("status") != "Delivered" else ""

            lines.append(
                f"Order #{o['_id']} | {o.get('restaurant_name', 'Unknown')} | "
                f"Status: {o.get('status', 'Unknown')} | ₹{o.get('total_amount', 0):,.2f} | "
                f"{ts_str}{time_note} | {refund_eligible}"
            )

        return f"User Orders (newest first):\n" + "\n".join(lines)
    except Exception as e:
        return f"Error querying database: {e}"


# ──────────────────────────────────────────────
# Tool 3: get_order_details
# ──────────────────────────────────────────────
async def get_order_details(order_id: int) -> str:
    """
    Fetches detailed information for a specific order by its ID,
    including delivery timestamp and refund eligibility.
    """
    try:
        order = await db.orders.find_one({"_id": order_id})
        if not order:
            return f"Order #{order_id} not found."

        ts = order.get("timestamp")
        now = datetime.now(timezone.utc)

        refund_eligible = False
        hours_since = None
        if ts and order.get("status") == "Delivered":
            ts_utc = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
            delta = now - ts_utc
            hours_since = delta.total_seconds() / 3600
            refund_eligible = hours_since <= REFUND_WINDOW_HOURS

        return (
            f"Order Details:\n"
            f"  Order ID: #{order['_id']}\n"
            f"  User ID: {order.get('user_id')}\n"
            f"  Restaurant: {order.get('restaurant_name', 'Unknown')}\n"
            f"  Status: {order.get('status', 'Unknown')}\n"
            f"  Amount: ₹{order.get('total_amount', 0):,.2f}\n"
            f"  Timestamp: {ts.strftime('%d %b %Y, %I:%M %p') if ts else 'Unknown'}\n"
            f"  Hours Since Delivery: {f'{hours_since:.1f}' if hours_since is not None else 'N/A'}\n"
            f"  Refund Eligible: {'Yes ✅' if refund_eligible else 'No ❌ — past 2-hour window or not delivered'}"
        )
    except Exception as e:
        return f"Error fetching order details: {e}"


# ──────────────────────────────────────────────
# Tool 4: initiate_refund (overhauled)
# ──────────────────────────────────────────────
async def initiate_refund(user_id: int, order_id: int, reason: str) -> str:
    """
    Initiates a refund for a user's order.
    Enforces the 2-hour refund window and calculates partial refund
    based on customer lifetime value (LTV).
    """
    try:
        order = await db.orders.find_one({"_id": order_id, "user_id": user_id})

        if not order:
            return "Order not found. Cannot process refund. The order ID might be wrong or doesn't belong to this user."

        # ── Rule 1: Only delivered orders can be refunded ──
        if order.get("status") != "Delivered":
            status = order.get("status", "Unknown")
            if status == "Refund Processing":
                return "This order already has a refund in progress."
            return f"Cannot refund this order — its status is '{status}'. Only delivered orders are eligible for refund."

        # ── Rule 2: 2-hour window enforcement ──
        ts = order.get("timestamp")
        if ts:
            now = datetime.now(timezone.utc)
            ts_utc = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
            hours_since = (now - ts_utc).total_seconds() / 3600

            if hours_since > REFUND_WINDOW_HOURS:
                return (
                    f"❌ Refund DENIED. This order was delivered {hours_since:.1f} hours ago, "
                    f"which is past the {REFUND_WINDOW_HOURS}-hour refund window. "
                    f"The customer can file a complaint instead, or you can escalate to a human agent."
                )
        else:
            return "Cannot verify delivery time for this order. Please escalate to a human agent."

        # ── Rule 3: Calculate refund based on LTV ──
        pipeline = [
            {"$match": {"user_id": user_id}},
            {"$group": {"_id": None, "total_spend": {"$sum": "$total_amount"}}}
        ]
        ltv_result = await db.orders.aggregate(pipeline).to_list(length=1)
        total_ltv = ltv_result[0]["total_spend"] if ltv_result else 0

        refund_pct, tier_reason = _get_refund_percentage(total_ltv)
        order_amount = order.get("total_amount", 0)
        refund_amount = round(order_amount * refund_pct, 2)

        # ── Create support ticket ──
        ticket_count = await db.supporttickets.count_documents({})
        new_ticket_id = ticket_count + 1

        await db.supporttickets.insert_one({
            "_id": new_ticket_id,
            "user_id": user_id,
            "order_id": order_id,
            "issue_type": "Refund",
            "status": "Open",
            "transcript": reason,
            "refund_amount": refund_amount,
            "refund_percentage": refund_pct,
            "ltv_at_time": total_ltv,
        })

        # ── Update order status ──
        await db.orders.update_one({"_id": order_id}, {"$set": {"status": "Refund Processing"}})

        # ── Credit wallet ──
        await db.users.update_one({"_id": user_id}, {"$inc": {"wallet_balance": refund_amount}})

        return (
            f"✅ Refund processed! Ticket #{new_ticket_id}\n"
            f"  Order amount: ₹{order_amount:,.2f}\n"
            f"  Refund amount: ₹{refund_amount:,.2f} ({int(refund_pct*100)}% of order)\n"
            f"  Reason: {tier_reason}\n"
            f"  The amount has been added to the customer's wallet."
        )

    except Exception as e:
        return f"Error processing refund: {e}"


# ──────────────────────────────────────────────
# Tool 5: file_complaint
# ──────────────────────────────────────────────
async def file_complaint(user_id: int, order_id: int, category: str, description: str) -> str:
    """
    Files a structured complaint against a specific order.
    Categories: food_quality, late_delivery, missing_items, wrong_order, hygiene, other
    """
    valid_categories = ["food_quality", "late_delivery", "missing_items", "wrong_order", "hygiene", "other"]
    if category not in valid_categories:
        category = "other"

    try:
        # Verify order exists and belongs to user
        order = await db.orders.find_one({"_id": order_id, "user_id": user_id})
        if not order:
            return "Order not found or doesn't belong to this user. Cannot file complaint."

        # Check for duplicate complaint
        existing = await db.complaints.find_one({"user_id": user_id, "order_id": order_id, "category": category})
        if existing:
            return f"A complaint for this order with category '{category}' already exists (Complaint #{existing['_id']}). No duplicate filed."

        complaint_count = await db.complaints.count_documents({})
        new_complaint_id = complaint_count + 1

        await db.complaints.insert_one({
            "_id": new_complaint_id,
            "user_id": user_id,
            "order_id": order_id,
            "category": category,
            "description": description,
            "status": "Open",
            "created_at": datetime.now(timezone.utc),
        })

        return (
            f"✅ Complaint #{new_complaint_id} filed successfully.\n"
            f"  Order: #{order_id} ({order.get('restaurant_name', 'Unknown')})\n"
            f"  Category: {category}\n"
            f"  Description: {description}\n"
            f"  Status: Open — our team will review this."
        )
    except Exception as e:
        return f"Error filing complaint: {e}"


# ──────────────────────────────────────────────
# Tool 6: escalate_to_human
# ──────────────────────────────────────────────
async def escalate_to_human(user_id: int, reason: str) -> str:
    """
    Escalates the current conversation to a human agent.
    Creates an escalation ticket and returns a confirmation.
    """
    try:
        ticket_count = await db.supporttickets.count_documents({})
        new_ticket_id = ticket_count + 1

        await db.supporttickets.insert_one({
            "_id": new_ticket_id,
            "user_id": user_id,
            "issue_type": "Escalation",
            "status": "Escalated",
            "transcript": reason,
        })

        return (
            f"🔔 Escalation ticket #{new_ticket_id} created.\n"
            f"  A human agent will contact the customer within 30 minutes.\n"
            f"  Reason: {reason}\n"
            f"  Please assure the customer that their issue is being prioritized."
        )
    except Exception as e:
        return f"Error creating escalation: {e}"
