
import json
import urllib.request
import urllib.parse

BOT_TOKEN = "8785235717:AAEXu9JRb1NRd8Azz5PhC_DHQLWwSHyzDiE"
URL = f"https://api.telegram.org/bot{BOT_TOKEN}/"

def send_request(method, params=None):
    if params:
        data = urllib.parse.urlencode(params).encode('utf-8')
        req = urllib.request.Request(URL + method, data=data)
    else:
        req = urllib.request.Request(URL + method)
    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception as e:
        print(f"Xatolik: {e}")
        return None

def approve_user(chat_id, user_id):
    res = send_request("approveChatJoinRequest", {
        "chat_id": chat_id,
        "user_id": user_id
    })
    if res and res.get("ok"):
        print(f"Tasdiqlandi! Chat ID: {chat_id}, User ID: {user_id}")

def main():
    print("Bot muvaffaqiyatli ishga tushdi va kutilmoqda...")
    offset = 0
    while True:
        updates = send_request("getUpdates", {"offset": offset, "timeout": 30})
        if updates and updates.get("ok"):
            for update in updates.get("result", []):
                offset = update["update_id"] + 1
                if "chat_join_request" in update:
                    req = update["chat_join_request"]
                    chat_id = req["chat"]["id"]
                    user_id = req["from"]["id"]
                    approve_user(chat_id, user_id)
        

if __name__ == "__main__":
    main()

