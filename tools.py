import os
from dotenv import load_dotenv
import motor.motor_asyncio

# Load .env from the parent directory to ensure MONGODB_URI is always available, even if run independently
dotenv_path = os.path.join(os.path.dirname(__file__), '..', '.env')
load_dotenv(dotenv_path)

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/test")
client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URI)
# Use the database specified in the URI, or default to "test" (which Mongoose uses by default)
db = client.get_default_database(default="test")

async def check_order_status(user_id: int) -> str:
    """
    Checks the active and historical order status for a given user.
    Uses motor to connect directly to MongoDB.
    """
    try:
        cursor = db.orders.find({"user_id": user_id})
        orders = await cursor.to_list(length=100)
        
        if not orders:
            return "The user has no past or active orders."
            
        summary = "\n".join([
            f"Order #{o['_id']} at {o.get('restaurant_name', 'Unknown')} - Status: {o.get('status', 'Unknown')} - Total: ₹{o.get('total_amount', 0)}"
            for o in orders
        ])
        return f"User Orders:\n{summary}"
    except Exception as e:
        return f"Error querying database: {e}"

async def initiate_refund(user_id: int, order_id: int, reason: str) -> str:
    """
    Initiates a refund for a user's order and logs the complaint transcript directly via MongoDB.
    """
    try:
        order = await db.orders.find_one({"_id": order_id, "user_id": user_id})
        
        if not order:
            return "Order not found. Cannot process refund. The order ID might be wrong."
            
        # create support ticket
        ticket_count = await db.supporttickets.count_documents({})
        new_ticket_id = ticket_count + 1
        
        await db.supporttickets.insert_one({
             "_id": new_ticket_id,
             "user_id": user_id,
             "order_id": order_id,
             "issue_type": "Refund",
             "status": "Open",
             "transcript": reason
        })
        
        await db.orders.update_one({"_id": order_id}, {"$set": {"status": "Refund Processing"}})
        
        refund_amount = order.get("total_amount", 0)
        await db.users.update_one({"_id": user_id}, {"$inc": {"wallet_balance": refund_amount}})
        
        return f"Refund ticket #{new_ticket_id} has been created and the refund is processing."
        
    except Exception as e:
        return f"Error contacting data backend: {e}"
