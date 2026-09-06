DEFAULT_TIMEOUT = 30


class UserService:
    def __init__(self, db):
        self.db = db

    def get_user(self, user_id):
        return self.db.fetch(user_id)


def helper():
    pass
