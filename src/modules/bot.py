"""An interpreter that reads and executes user-created routines."""

import ast
import threading
import time
import git
import cv2
import inspect
import importlib
import traceback
from queue import Empty, Full, Queue
from os.path import splitext, basename
from src.common import config, utils
from src.common import vkeys
from src.common.hid_keyboard import KeyboardOutputError, key_usage
from src.detection import detection
from src.detection.minimap_rune import find_runes
from src.routine import components
from src.routine.routine import Routine
from src.command_book.command_book import CommandBook
from src.routine.components import Point
from src.common.vkeys import press, click
from src.common.interfaces import Configurable
from src.modules.usb_video import STALE_AFTER


# The rune's buff icon
RUNE_BUFF_TEMPLATE = cv2.imread('assets/rune_buff_template.jpg', 0)


class Bot(Configurable):
    """A class that interprets and executes user-defined routines."""

    DEFAULT_CONFIG = {
        'Interact': 'y',
        'Feed pet': '9'
    }

    def __init__(self):
        """Loads a user-defined routine on start up and initializes this Bot's main thread."""

        super().__init__('keybindings')
        config.bot = self

        self.rune_active = False
        self.rune_pos = (0, 0)
        self.rune_closest_pos = (0, 0)      # Location of the Point closest to rune
        self.submodules = []
        self.command_book = None            # CommandBook instance
        self.executing_step = False
        self.usb_rune_requests = Queue(maxsize=1)
        self.usb_rune_result = None
        self.model_error = None
        # self.module_name = None
        # self.buff = components.Buff()

        # self.command_book = {}
        # for c in (components.Wait, components.Walk, components.Fall,
        #           components.Move, components.Adjust, components.Buff):
        #     self.command_book[c.__name__.lower()] = c

        config.routine = Routine()

        self.ready = False
        self.thread = threading.Thread(target=self._main)
        self.thread.daemon = True

    def start(self):
        """
        Starts this Bot object's thread.
        :return:    None
        """

        self.update_submodules()
        print('\n[~] Started main bot loop')
        self.thread.start()

    def _main(self):
        """
        The main body of Bot that executes the user's routine.
        :return:    None
        """

        print('\n[~] Initializing detection algorithm:\n')
        try:
            model = detection.load_model()
        except (OSError, ValueError) as error:
            self.model_error = str(error)
            print(f'\n[!] Rune model unavailable: {error}')
            return
        print('\n[~] Initialized detection algorithm')

        self.ready = True
        config.listener.enabled = True
        last_fed = time.time()
        while True:
            if config.capture_mode != config.CAPTURE_MODE and not vkeys.dry_running():
                self._inspect_video_rune(model)
                time.sleep(0.01)
            elif config.enabled and len(config.routine) > 0:
                with vkeys.input_lock:
                    if not config.enabled:
                        continue
                    self.executing_step = True
                try:
                    last_fed = self._run_step(model, last_fed)
                except KeyboardOutputError as error:
                    vkeys.disarm_leonardo()
                    vkeys.stop_dry_run(str(error))
                    print(f'[Input] {error}')
                except Exception:
                    vkeys.stop_dry_run('Dry routine failed.')
                    raise
                finally:
                    self.executing_step = False
            else:
                time.sleep(0.01)

    def _run_step(self, model, last_fed):
        dry = vkeys.dry_running()
        if dry:
            observation = vkeys.refresh_dry_run()
            if observation is None:
                return last_fed
            minimap = observation.minimap_pixels()
            points = [item for item in config.routine.sequence if isinstance(item, Point)]
            matches = find_runes(minimap) if points else []
            self.rune_active = bool(matches)
            if matches:
                self.rune_pos = utils.convert_to_relative(matches[0], minimap)
                self.rune_closest_pos = min(
                    (p.location for p in points), key=lambda pos: utils.distance(pos, self.rune_pos))
        self.command_book.buff.main()
        auto_feed, num_pets = config.gui.pet_options
        now = time.time()
        if auto_feed and now - last_fed > 1200 / num_pets:
            press(self.config['Feed pet'], 1)
            last_fed = now

        config.gui.routine_highlight = config.routine.index
        element = config.routine[config.routine.index]
        if self.rune_active and isinstance(element, Point) and element.location == self.rune_closest_pos:
            self._solve_rune(model)
        element.execute()
        config.routine.step()
        return last_fed

    def validate_keyboard_only(self):
        book = self.command_book
        if book is None or not len(config.routine):
            raise KeyboardOutputError('Load a keyboard-only command book and a routine first.')
        for key in (*self.config.values(), *book.config.values()):
            key_usage(key)
        try:
            tree = ast.parse(inspect.getsource(book.module))
        except (OSError, SyntaxError, TypeError) as error:
            raise KeyboardOutputError('Cannot inspect the command book for mouse commands.') from error
        for node in ast.walk(tree):
            name = getattr(node, 'attr', None) or getattr(node, 'id', None) or getattr(node, 'name', None)
            if name in ('click', 'mouse_event', 'SetCursorPos'):
                raise KeyboardOutputError('The loaded command book references mouse output.')
            if isinstance(node, ast.alias) and node.name.split('.')[0] in (
                    'ctypes', 'keyboard', 'mouse', 'pyautogui', 'pydirectinput', 'pynput', 'win32api'):
                raise KeyboardOutputError('Keyboard-only Command Books must send input through vkeys.')
            if isinstance(node, ast.ImportFrom) and (node.module or '').split('.')[0] in (
                    'ctypes', 'keyboard', 'mouse', 'pyautogui', 'pydirectinput', 'pynput', 'win32api'):
                raise KeyboardOutputError('Keyboard-only Command Books must send input through vkeys.')
            if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
                operation = getattr(node.func, 'attr', None) or getattr(node.func, 'id', None)
                if operation in ('key_down', 'key_up', 'press'):
                    key_usage(node.args[0].value)

    def inspect_video_rune(self, observation):
        if not self.ready:
            raise ValueError('Rune model unavailable.' if self.model_error else 'Rune model is loading.')
        try:
            self.usb_rune_requests.put_nowait(observation)
        except Full as error:
            raise ValueError('A rune frame is already queued.') from error

    def _inspect_video_rune(self, model):
        try:
            observation = self.usb_rune_requests.get_nowait()
        except Empty:
            return
        frame = observation.frame

        def current_session():
            current = config.capture.usb_observation().frame
            return (config.capture_mode != config.CAPTURE_MODE and current is not None
                    and current.session == frame.session)

        if current_session() and time.monotonic() - frame.received < STALE_AFTER:
            try:
                result = ', '.join(detection.detect_rune(model, observation.roi)) or 'No arrows detected'
            except (cv2.error, ValueError, detection.tf.errors.OpError) as error:
                result = f'Rune detection failed: {error}'
            if current_session():
                self.usb_rune_result = frame.session, frame.number, result

    @utils.run_if_enabled
    def _solve_rune(self, model):
        """
        Moves to the position of the rune and solves the arrow-key puzzle.
        :param model:   The TensorFlow model to classify with.
        :param sct:     The mss instance object with which to take screenshots.
        :return:        None
        """

        move = self.command_book['move']
        move(*self.rune_pos).execute()
        adjust = self.command_book['adjust']
        adjust(*self.rune_pos).execute()
        time.sleep(0.2)
        press(self.config['Interact'], 1, down_time=0.2)        # Inherited from Configurable

        print('\nSolving rune:')
        inferences = []
        dry = vkeys.dry_running()
        previous_number = None
        for _ in range(15):
            if dry:
                observation = vkeys.refresh_dry_run()
                if observation is None:
                    return
                if observation.frame.number == previous_number:
                    time.sleep(.04)
                    continue
                previous_number = observation.frame.number
                frame = observation.roi
            else:
                frame = config.capture.frame
            solution = detection.merge_detection(model, frame)
            if solution:
                print(', '.join(solution))
                if solution in inferences:
                    print('Solution found, entering result')
                    external = vkeys.external_keyboard()
                    before_buff = self._rune_buff_visible() if external is not None and not dry else False
                    for arrow in solution:
                        press(arrow, 1, down_time=0.1)
                    if dry:
                        self.rune_active = False
                        break
                    if external is not None:
                        self._await_keyboard_rune_completion(before_buff)
                        self.rune_active = False
                        break
                    time.sleep(1)
                    for _ in range(3):
                        time.sleep(0.3)
                        frame = config.capture.frame
                        rune_buff = utils.multi_match(frame[:frame.shape[0] // 8, :],
                                                      RUNE_BUFF_TEMPLATE,
                                                      threshold=0.9)
                        if rune_buff:
                            rune_buff_pos = min(rune_buff, key=lambda p: p[0])
                            target = (
                                round(rune_buff_pos[0] + config.capture.window['left']),
                                round(rune_buff_pos[1] + config.capture.window['top'])
                            )
                            click(target, button='right')
                    self.rune_active = False
                    break
                elif len(solution) == 4:
                    inferences.append(solution)

    @staticmethod
    def _rune_buff_visible():
        frame = config.capture.frame
        local = config.capture.local_target
        if frame is None or local is None or time.monotonic() - local[1] >= STALE_AFTER:
            return False
        return bool(utils.multi_match(frame[:frame.shape[0] // 8, :], RUNE_BUFF_TEMPLATE, threshold=.9))

    def _await_keyboard_rune_completion(self, before_buff):
        for _ in range(6):
            time.sleep(.3)
            if not config.enabled:
                return
            if not before_buff and self._rune_buff_visible():
                print('[Leonardo] Rune completion confirmed by a new buff icon.')
                return
        print('[Leonardo] Rune completion could not be confirmed. Input stopped.')
        vkeys.disarm_leonardo()

    def load_commands(self, file):
        try:
            self.command_book = CommandBook(file)
            config.gui.settings.update_class_bindings()
        except ValueError:
            pass    # TODO: UI warning popup, say check cmd for errors
        #
        # utils.print_separator()
        # print(f"[~] Loading command book '{basename(file)}':")
        #
        # ext = splitext(file)[1]
        # if ext != '.py':
        #     print(f" !  '{ext}' is not a supported file extension.")
        #     return False
        #
        # new_step = components.step
        # new_cb = {}
        # for c in (components.Wait, components.Walk, components.Fall):
        #     new_cb[c.__name__.lower()] = c
        #
        # # Import the desired command book file
        # module_name = splitext(basename(file))[0]
        # target = '.'.join(['resources', 'command_books', module_name])
        # try:
        #     module = importlib.import_module(target)
        #     module = importlib.reload(module)
        # except ImportError:     # Display errors in the target Command Book
        #     print(' !  Errors during compilation:\n')
        #     for line in traceback.format_exc().split('\n'):
        #         line = line.rstrip()
        #         if line:
        #             print(' ' * 4 + line)
        #     print(f"\n !  Command book '{module_name}' was not loaded")
        #     return
        #
        # # Check if the 'step' function has been implemented
        # step_found = False
        # for name, func in inspect.getmembers(module, inspect.isfunction):
        #     if name.lower() == 'step':
        #         step_found = True
        #         new_step = func
        #
        # # Populate the new command book
        # for name, command in inspect.getmembers(module, inspect.isclass):
        #     new_cb[name.lower()] = command
        #
        # # Check if required commands have been implemented and overridden
        # required_found = True
        # for command in [components.Buff]:
        #     name = command.__name__.lower()
        #     if name not in new_cb:
        #         required_found = False
        #         new_cb[name] = command
        #         print(f" !  Error: Must implement required command '{name}'.")
        #
        # # Look for overridden movement commands
        # movement_found = True
        # for command in (components.Move, components.Adjust):
        #     name = command.__name__.lower()
        #     if name not in new_cb:
        #         movement_found = False
        #         new_cb[name] = command
        #
        # if not step_found and not movement_found:
        #     print(f" !  Error: Must either implement both 'Move' and 'Adjust' commands, "
        #           f"or the function 'step'")
        # if required_found and (step_found or movement_found):
        #     self.module_name = module_name
        #     self.command_book = new_cb
        #     self.buff = new_cb['buff']()
        #     components.step = new_step
        #     config.gui.menu.file.enable_routine_state()
        #     config.gui.view.status.set_cb(basename(file))
        #     config.routine.clear()
        #     print(f" ~  Successfully loaded command book '{module_name}'")
        # else:
        #     print(f" !  Command book '{module_name}' was not loaded")

    def update_submodules(self, force=False):
        """
        Pulls updates from the submodule repositories. If FORCE is True,
        rebuilds submodules by overwriting all local changes.
        """

        utils.print_separator()
        print('[~] Retrieving latest submodules:')
        self.submodules = []
        repo = git.Repo.init()
        with open('.gitmodules', 'r') as file:
            lines = file.readlines()
            i = 0
            while i < len(lines):
                if lines[i].startswith('[') and i < len(lines) - 2:
                    path = lines[i + 1].split('=')[1].strip()
                    url = lines[i + 2].split('=')[1].strip()
                    self.submodules.append(path)
                    try:
                        repo.git.clone(url, path)       # First time loading submodule
                        print(f" -  Initialized submodule '{path}'")
                    except git.exc.GitCommandError:
                        sub_repo = git.Repo(path)
                        if not force:
                            sub_repo.git.stash()        # Save modified content
                        sub_repo.git.fetch('origin', 'main')
                        sub_repo.git.reset('--hard', 'FETCH_HEAD')
                        if not force:
                            try:                # Restore modified content
                                sub_repo.git.checkout('stash', '--', '.')
                                print(f" -  Updated submodule '{path}', restored local changes")
                            except git.exc.GitCommandError:
                                print(f" -  Updated submodule '{path}'")
                        else:
                            print(f" -  Rebuilt submodule '{path}'")
                        sub_repo.git.stash('clear')
                    i += 3
                else:
                    i += 1
