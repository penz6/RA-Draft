from core import app
from hra_pause import toggle_participant_pause

@app.route("/sessions/<int:session_id>/defer/<int:user_id>", methods=["POST"])
def defer_turn(session_id, user_id):
    return toggle_participant_pause(session_id, user_id)
