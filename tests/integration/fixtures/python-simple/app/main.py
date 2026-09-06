from .services import UserService
import os.path


def run():
    service = UserService(db=None)
    return service.get_user(1)
