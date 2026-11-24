"""The central program that ties all the modules together."""

def main():
    from src.common.vkeys import disarm_leonardo, stop_dry_run
    from src.modules.bot import Bot
    from src.modules.capture import Capture
    from src.modules.notifier import Notifier
    from src.modules.listener import Listener
    from src.modules.gui import GUI

    bot = Bot()
    capture = Capture()
    notifier = Notifier()
    listener = Listener()

    bot.start()
    gui = GUI()
    capture.start()
    notifier.start()
    listener.start()

    print('\n[~] Successfully initialized Auto Maple')
    try:
        gui.start()
    finally:
        stop_dry_run()
        disarm_leonardo(wait=True)
        capture.close()


if __name__ == '__main__':
    main()
