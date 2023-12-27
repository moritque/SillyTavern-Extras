"""THA3 live mode for SillyTavern-extras.

This is the animation engine, running on top of the THA3 posing engine.
This module implements the live animation backend and serves the API. For usage, see `server.py`.

If you want to play around with THA3 expressions in a standalone app, see `manual_poser.py`.
"""

import atexit
import io
import logging
import math
import os
import random
import sys
import time
import numpy as np
import threading
from typing import Dict, List, NoReturn, Optional, Union

import PIL

import torch
import torchvision

from flask import Flask, Response
from flask_cors import CORS

from tha3.poser.modes.load_poser import load_poser
from tha3.poser.poser import Poser
from tha3.util import (torch_linear_to_srgb, resize_PIL_image,
                       extract_PIL_image_from_filelike, extract_pytorch_image_from_PIL_image)
from tha3.app.util import posedict_keys, posedict_key_to_index, load_emotion_presets, posedict_to_pose, to_talkinghead_image, FpsStatistics

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------------
# Global variables

talkinghead_basedir = "talkinghead"

global_animator_instance = None
_animator_output_lock = threading.Lock()  # protect from concurrent access to `result_image` and the `frame_ready` flag.

# These need to be written to by the API functions.
#
# Since the plugin might not have been started yet at that time (so the animator instance might not exist),
# it's better to keep this state in module-level globals rather than in attributes of the animator.
animation_running = False  # used in initial bootup state, and while loading a new image
current_emotion = "neutral"
is_talking = False
global_reload_image = None

# --------------------------------------------------------------------------------
# API

# Flask setup
app = Flask(__name__)
CORS(app)

def setEmotion(_emotion: Dict[str, float]) -> None:
    """Set the current emotion of the character based on sentiment analysis results.

    Currently, we pick the emotion with the highest confidence score.

    _emotion: result of sentiment analysis: {emotion0: confidence0, ...}
    """
    global current_emotion

    highest_score = float("-inf")
    highest_label = None

    for item in _emotion:
        if item["score"] > highest_score:
            highest_score = item["score"]
            highest_label = item["label"]

    logger.debug(f"setEmotion: applying emotion {highest_label}")
    current_emotion = highest_label

def unload() -> str:
    """Stop animation."""
    global animation_running
    animation_running = False
    logger.debug("unload: animation paused")
    return "Animation Paused"

def start_talking() -> str:
    """Start talking animation."""
    global is_talking
    is_talking = True
    logger.debug("start_talking called")
    return "started"

def stop_talking() -> str:
    """Stop talking animation."""
    global is_talking
    is_talking = False
    logger.debug("stop_talking called")
    return "stopped"

def result_feed() -> Response:
    """Return a Flask `Response` that repeatedly yields the current image as 'image/png'."""
    def generate():
        last_update_time = None
        last_report_time = None
        fps_statistics = FpsStatistics()
        image_bytes = None

        while True:
            # Retrieve a new frame from the animator if available.
            have_new_frame = False
            with _animator_output_lock:
                if global_animator_instance.frame_ready:
                    image_rgba = global_animator_instance.result_image
                    try:
                        pil_image = PIL.Image.fromarray(np.uint8(image_rgba[:, :, :3]))
                        if image_rgba.shape[2] == 4:
                            alpha_channel = image_rgba[:, :, 3]
                            pil_image.putalpha(PIL.Image.fromarray(np.uint8(alpha_channel)))
                        global_animator_instance.frame_ready = False  # Animation frame consumed; tell the animator it can begin rendering the next one.
                        have_new_frame = True
                    except Exception as exc:
                        logger.error(exc)

            # Pack the new animation frame for sending.
            if have_new_frame:
                try:
                    buffer = io.BytesIO()  # Save as PNG with RGBA mode
                    pil_image.save(buffer, format="PNG")
                    image_bytes = buffer.getvalue()
                except Exception as exc:
                    logger.error(f"Cannot write image to buffer: {exc}")
                    raise

            # Send the animation frame.
            if image_bytes is not None:
                # How often should we send?
                #  - Excessive spamming can DoS the SillyTavern GUI, so there needs to be a rate limit.
                #  - OTOH, we must constantly send something, or the GUI will lock up waiting.
                #
                # Thus, if we have a new frame, or enough time has elapsed already (slow GPU or running on CPU), send it now. Otherwise wait for a bit.
                # Target an acceptable anime frame rate of 25 FPS.
                TARGET_TIME_SEC = 0.04  # 1/25
                if last_update_time is not None:
                    time_now = time.time_ns()
                    elapsed_time = time_now - last_update_time
                    past_frame_deadline = (elapsed_time / 10**9) > TARGET_TIME_SEC
                else:
                    past_frame_deadline = True  # nothing rendered yet

                if have_new_frame or past_frame_deadline:
                    yield (b"--frame\r\n"
                           b"Content-Type: image/png\r\n\r\n" + image_bytes + b"\r\n")

                    # Update the FPS counter, measuring the time between network sends.
                    time_now = time.time_ns()
                    if last_update_time is not None:
                        elapsed_time = time_now - last_update_time
                        fps = 1.0 / (elapsed_time / 10**9)
                        fps_statistics.add_fps(fps)
                    last_update_time = time_now
                else:
                    # We don't measure pack/send time, so this is not exact. In practice the resulting framerate is slightly under the target (24 vs. 25 FPS).
                    # Note the animator runs in a different thread, so it can render while we are waiting.
                    time.sleep(TARGET_TIME_SEC)

                # Log the FPS counter in 5-second intervals.
                if last_report_time is None or time_now - last_report_time > 5e9:
                    trimmed_fps = round(fps_statistics.get_average_fps(), 1)
                    logger.info("rate-limited network FPS: {:.1f}".format(trimmed_fps))
                    last_report_time = time_now

            else:  # first frame not yet available, animator still booting
                time.sleep(0.1)

    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")

# TODO: the input is a flask.request.file.stream; what's the type of that?
def talkinghead_load_file(stream) -> str:
    """Load image from stream and start animation."""
    global global_reload_image
    global animation_running
    logger.debug("talkinghead_load_file: loading new input image from stream")

    try:
        animation_running = False  # pause animation while loading a new image
        pil_image = PIL.Image.open(stream)  # Load the image using PIL.Image.open
        img_data = io.BytesIO()  # Create a copy of the image data in memory using BytesIO
        pil_image.save(img_data, format="PNG")
        global_reload_image = PIL.Image.open(io.BytesIO(img_data.getvalue()))  # Set the global_reload_image to a copy of the image data
    except PIL.Image.UnidentifiedImageError:
        logger.warning("Could not load input image from stream, loading blank")
        full_path = os.path.join(os.getcwd(), os.path.normpath(os.path.join(talkinghead_basedir, "tha3", "images", "inital.png")))
        global_reload_image = PIL.Image.open(full_path)
    finally:
        animation_running = True
    return "OK"

def launch(device: str, model: str) -> Union[None, NoReturn]:
    """Launch the talking head plugin (live mode).

    If the plugin fails to load, the process exits.

    device: "cpu" or "cuda"
    model: one of the folder names inside "talkinghead/tha3/models/"
    """
    global global_animator_instance

    try:
        # If the animator already exists, clean it up first
        if global_animator_instance is not None:
            logger.info(f"launch: relaunching on device {device} with model {model}")
            global_animator_instance.exit()
            global_animator_instance = None

        poser = load_poser(model, device, modelsdir=os.path.join(talkinghead_basedir, "tha3", "models"))
        global_animator_instance = TalkingheadAnimator(poser, device)

        # Load initial blank character image
        full_path = os.path.join(os.getcwd(), os.path.normpath(os.path.join(talkinghead_basedir, "tha3", "images", "inital.png")))
        global_animator_instance.load_image(full_path)

        global_animator_instance.start()

    except RuntimeError as exc:
        logger.error(exc)
        sys.exit()

# --------------------------------------------------------------------------------
# Internal stuff

def convert_linear_to_srgb(image: torch.Tensor) -> torch.Tensor:
    """RGBA (linear) -> RGBA (SRGB), preserving the alpha channel."""
    rgb_image = torch_linear_to_srgb(image[0:3, :, :])
    return torch.cat([rgb_image, image[3:4, :, :]], dim=0)

class TalkingheadAnimator:
    """uWu Waifu"""

    def __init__(self, poser: Poser, device: torch.device):
        self.poser = poser
        self.device = device

        self.reset_animation_state()

        self.fps_statistics = FpsStatistics()

        self.source_image: Optional[torch.tensor] = None
        self.result_image: Optional[np.array] = None
        self.frame_ready = False
        self.last_report_time = None

        self.emotions, self.emotion_names = load_emotion_presets(os.path.join("talkinghead", "emotions"))

    # --------------------------------------------------------------------------------
    # Management

    def reset_animation_state(self):
        """Reset character state trackers for all animation drivers."""
        self.current_pose = None

        self.last_emotion = None
        self.last_emotion_change_timestamp = None

        self.last_blink_timestamp = None
        self.blink_interval = None

        self.last_sway_target_timestamp = None
        self.last_sway_target_pose = None
        self.sway_interval = None

        self.breathing_epoch = time.time_ns()

        self.frame_no = 0

    def load_image(self, file_path=None) -> None:
        """Load the image file at `file_path`, and replace the current character with it.

        Except, if `global_reload_image is not None`, use the global reload image data instead.
        In that case `file_path` is not used.

        When done, this always sets `global_reload_image` to `None`.
        """
        global global_reload_image

        try:
            if global_reload_image is not None:
                pil_image = global_reload_image
            else:
                pil_image = resize_PIL_image(
                    extract_PIL_image_from_filelike(file_path),
                    (self.poser.get_image_size(), self.poser.get_image_size()))

            w, h = pil_image.size

            if pil_image.size != (512, 512):
                logger.info("Resizing Char Card to work")
                pil_image = to_talkinghead_image(pil_image)

            w, h = pil_image.size

            if pil_image.mode != "RGBA":
                logger.error("load_image: image must have alpha channel")
                self.source_image = None
            else:
                self.source_image = extract_pytorch_image_from_PIL_image(pil_image) \
                    .to(self.device).to(self.poser.get_dtype())

        except Exception as exc:
            logger.error(f"load_image: {exc}")

        finally:
            global_reload_image = None

    def start(self) -> None:
        """Start the animation thread."""
        self._terminated = False
        def animation_update():
            while not self._terminated:
                self.render_animation_frame()
                time.sleep(0.01)  # rate-limit the renderer to 100 FPS maximum (this could be adjusted later)
        self.animation_thread = threading.Thread(target=animation_update, daemon=True)
        self.animation_thread.start()
        atexit.register(self.exit)

    def exit(self) -> None:
        """Terminate the animation thread.

        Called automatically when the process exits.
        """
        self._terminated = True
        self.animation_thread.join()

    # --------------------------------------------------------------------------------
    # Animation drivers

    def apply_emotion_to_pose(self, emotion_posedict: Dict[str, float], pose: List[float]) -> List[float]:
        """Copy all morphs except breathing from `emotion_posedict` to `pose`.

        If a morph does not exist in `emotion_posedict`, its value is copied from the original `pose`.

        Return the modified pose.
        """
        new_pose = list(pose)  # copy
        for idx, key in enumerate(posedict_keys):
            if key in emotion_posedict and key != "breathing_index":
                new_pose[idx] = emotion_posedict[key]
        return new_pose

    def animate_blinking(self, pose: List[float]) -> List[float]:
        """Eye blinking animation driver.

        Return the modified pose.
        """
        should_blink = (random.random() <= 0.03)

        # Prevent blinking too fast in succession.
        time_now = time.time_ns()
        if self.blink_interval is not None:
            # ...except when the "confusion" emotion has been entered recently.
            seconds_since_last_emotion_change = (time_now - self.last_emotion_change_timestamp) / 10**9
            if current_emotion == "confusion" and seconds_since_last_emotion_change < 10.0:
                pass
            else:
                seconds_since_last_blink = (time_now - self.last_blink_timestamp) / 10**9
                if seconds_since_last_blink < self.blink_interval:
                    should_blink = False

        if not should_blink:
            return pose

        # If there should be a blink, set the wink morphs to 1.
        new_pose = list(pose)  # copy
        for morph_name in ["eye_wink_left_index", "eye_wink_right_index"]:
            idx = posedict_key_to_index[morph_name]
            new_pose[idx] = 1.0

        # Typical for humans is 12...20 times per minute, i.e. 5...3 seconds interval.
        self.last_blink_timestamp = time_now
        self.blink_interval = random.uniform(2.0, 5.0)  # seconds; duration of this blink before the next one can begin

        return new_pose

    def animate_talking(self, pose: List[float]) -> List[float]:
        """Talking animation driver.

        Works by randomizing the mouth-open state.

        Return the modified pose.
        """
        if not is_talking:
            return pose

        # TODO: improve talking animation once we get the client to actually use it
        new_pose = list(pose)  # copy
        idx = posedict_key_to_index["mouth_aaa_index"]
        x = pose[idx]
        x = abs(1.0 - x) + random.uniform(-2.0, 2.0)
        x = max(0.0, min(x, 1.0))  # clamp (not the manga studio)
        new_pose[idx] = x
        return new_pose

    def compute_sway_target_pose(self, original_target_pose: List[float]) -> List[float]:
        """History-free sway animation driver.

        original_target_pose: emotion pose to modify with a randomized sway target

        The target is randomized again when necessary; this takes care of caching internally.

        Return the modified pose.
        """
        # We just modify the target pose, and let the integrator (`interpolate_pose`) do the actual animation.
        # - This way we don't need to track start state, progress, etc.
        # - This also makes the animation nonlinear automatically: a saturating exponential trajectory toward the target.
        #     - If we want to add a smooth start, we'll need a ramp-in mechanism to interpolate the target from the current pose to the actual target gradually.
        #       The nonlinearity automatically takes care of slowing down when the target is approached.

        random_max = 0.6  # max sway magnitude from center position of each morph
        noise_max = 0.02  # amount of dynamic noise (re-generated every frame), added on top of the sway target

        SWAYPARTS = ["head_x_index", "head_y_index", "neck_z_index", "body_y_index", "body_z_index"]

        def macrosway() -> List[float]:  # this handles caching and everything
            time_now = time.time_ns()
            should_pick_new_sway_target = True
            if current_emotion == self.last_emotion:
                if self.sway_interval is not None:  # have we created a swayed pose at least once?
                    seconds_since_last_sway_target = (time_now - self.last_sway_target_timestamp) / 10**9
                    if seconds_since_last_sway_target < self.sway_interval:
                        should_pick_new_sway_target = False
            # else, emotion has changed, invalidating the old sway target, because it is based on the old emotion.

            if not should_pick_new_sway_target:
                if self.last_sway_target_pose is not None:  # When keeping the same sway target, return the cached sway pose if we have one.
                    return self.last_sway_target_pose
                else:  # Should not happen, but let's be robust.
                    return original_target_pose

            new_target_pose = list(original_target_pose)  # copy
            for key in SWAYPARTS:
                idx = posedict_key_to_index[key]
                target_value = original_target_pose[idx]

                # Determine the random range so that the swayed target always stays within `[-random_max, random_max]`, regardless of `target_value`.
                # TODO: This is a simple zeroth-order solution that just cuts the random range.
                #       Would be nicer to *gradually* decrease the available random range on the "outside" as the target value gets further from the origin.
                random_upper = max(0, random_max - target_value)  # e.g. if target_value = 0.2, then random_upper = 0.4  => max possible = 0.6 = random_max
                random_lower = min(0, -random_max - target_value)  # e.g. if target_value = -0.2, then random_lower = -0.4  => min possible = -0.6 = -random_max
                random_value = random.uniform(random_lower, random_upper)

                new_target_pose[idx] = target_value + random_value

            self.last_sway_target_pose = new_target_pose
            self.last_sway_target_timestamp = time_now
            self.sway_interval = random.uniform(5.0, 10.0)  # seconds; duration of this sway target before randomizing new one
            return new_target_pose

        # Add dynamic noise (re-generated every frame) to the target to make the animation look less robotic, especially once we are near the target pose.
        def add_microsway() -> None:  # DANGER: MUTATING FUNCTION
            for key in SWAYPARTS:
                idx = posedict_key_to_index[key]
                x = new_target_pose[idx] + random.uniform(-noise_max, noise_max)
                x = max(-1.0, min(x, 1.0))
                new_target_pose[idx] = x

        new_target_pose = macrosway()
        add_microsway()
        return new_target_pose

    def animate_breathing(self, pose: List[float]) -> List[float]:
        """Breathing animation driver.

        Return the modified pose.
        """
        breathing_cycle_duration = 4.0  # seconds

        time_now = time.time_ns()
        t = (time_now - self.breathing_epoch) / 10**9  # seconds since breathing-epoch
        cycle_pos = t / breathing_cycle_duration  # number of cycles since breathing-epoch
        if cycle_pos > 1.0:  # prevent loss of accuracy in long sessions
            self.breathing_epoch = time_now  # TODO: be more accurate here, should sync to a whole cycle
        cycle_pos = cycle_pos - float(int(cycle_pos))  # fractional part

        new_pose = list(pose)  # copy
        idx = posedict_key_to_index["breathing_index"]
        new_pose[idx] = math.sin(cycle_pos * math.pi)**2  # 0 ... 1 ... 0, smoothly, with slow start and end, fast middle
        return new_pose

    def interpolate_pose(self, pose: List[float], target_pose: List[float], step: float = 0.1) -> List[float]:
        """Rate-based pose integrator. Interpolate from `pose` toward `target_pose`.

        `step`: [0, 1]; how far toward `target_pose` to interpolate. 0 is fully `pose`, 1 is fully `target_pose`.

        Note that looping back the output as `pose`, while keeping `target_pose` constant, causes the current pose
        to approach `target_pose` on a saturating exponential trajectory, like `1 - exp(-lambda * t)`, for some
        constant `lambda`.

        This is because `step` is the fraction of the *current* difference between `pose` and `target_pose`,
        which obviously becomes smaller after each repeat. This is a feature, not a bug!

        This is a kind of history-free rate-based formulation, which needs only the current and target poses, and
        the step size; there is no need to keep track of e.g. the initial pose or the progress along the trajectory.
        """
        # NOTE: This overwrites blinking, talking, and breathing, but that doesn't matter, because we apply this first.
        # The other animation drivers then modify our result.
        new_pose = list(pose)  # copy
        for idx, key in enumerate(posedict_keys):
            # # We now animate blinking *after* interpolating the pose, so when blinking, the eyes close instantly.
            # # This modification would make the blink also end instantly.
            # if key in ["eye_wink_left_index", "eye_wink_right_index"]:
            #     new_pose[idx] = target_pose[idx]
            # else:
            #     ...

            delta = target_pose[idx] - pose[idx]
            new_pose[idx] = pose[idx] + step * delta
        return new_pose

    # --------------------------------------------------------------------------------
    # Animation logic

    def render_animation_frame(self) -> None:
        """Render an animation frame.

        If the previous rendered frame has not been retrieved yet, do nothing.
        """
        if not animation_running:
            return

        # If no one has retrieved the previous frame yet, do not render a new one.
        if self.frame_ready:
            return

        if global_reload_image is not None:
            self.load_image()
        if self.source_image is None:
            return

        time_render_start = time.time_ns()

        if self.current_pose is None:  # initialize character pose at plugin startup
            self.current_pose = posedict_to_pose(self.emotions[current_emotion])

        emotion_posedict = self.emotions[current_emotion]
        if current_emotion != self.last_emotion:  # some animation drivers need to know when the emotion last changed
            self.last_emotion_change_timestamp = time_render_start

        target_pose = self.apply_emotion_to_pose(emotion_posedict, self.current_pose)
        target_pose = self.compute_sway_target_pose(target_pose)

        self.current_pose = self.interpolate_pose(self.current_pose, target_pose)
        self.current_pose = self.animate_blinking(self.current_pose)
        self.current_pose = self.animate_talking(self.current_pose)
        self.current_pose = self.animate_breathing(self.current_pose)

        # Update this last so that animation drivers have access to the old emotion, too.
        self.last_emotion = current_emotion

        pose = torch.tensor(self.current_pose, device=self.device, dtype=self.poser.get_dtype())

        with torch.no_grad():
            # - [0]: model's output index for the full result image
            # - model's data range is [-1, +1], linear intensity ("gamma encoded")
            output_image = self.poser.pose(self.source_image, pose)[0].float()
            # output_image = (output_image + 1.0) / 2.0  # -> [0, 1]
            output_image.add_(1.0)
            output_image.mul_(0.5)

            c, h, w = output_image.shape

            # --------------------------------------------------------------------------------
            # Postproc filters
            #
            # Let the glitch artistry begin.

            # TODO:
            #   - refactor this into a `postprocessor.py`
            #   - make configurable (ask Cohee1207 about the preferable way)
            #   - create the base meshgrid for image coordinates only once (not once per effect per frame, as now)

            def apply_bloom(image: torch.tensor, luma_threshold: float = 0.8, hdr_exposure: float = 0.7) -> None:
                """Bloom effect (fake HDR). Popular in early 2000s anime.

                Bright parts of the image bleed light into their surroundings, creating enhanced perceived contrast.
                Only makes sense when the character is rendered on a dark-ish background.

                `luma_threshold`: How bright is bright. 0.0 is full black, 1.0 is full white.
                `hdr_exposure`: Controls the overall brightness of the output. Like in photography,
                                higher exposure means brighter image (saturating toward white).
                """
                # There are online tutorials for how to create this effect, see e.g.:
                #   https://learnopengl.com/Advanced-Lighting/Bloom

                # Find the bright parts.
                Y = 0.2126 * image[0, :, :] + 0.7152 * image[1, :, :] + 0.0722 * image[2, :, :]  # HDTV luminance
                mask = torch.ge(Y, luma_threshold)  # [h, w]

                # Make a copy of the image with just the bright parts.
                mask = torch.unsqueeze(mask, 0)  # -> [1, h, w]
                brights = image * mask  # [c, h, w]

                # Blur the bright parts. Two-pass blur to save compute, since we need a very large blur kernel.
                # It seems that in Torch, one large 1D blur is faster than looping with a smaller one.
                #
                # Although everything else in Torch takes (height, width), kernel size is given as (size_x, size_y);
                # see `gaussian_blur_image` in https://pytorch.org/vision/main/_modules/torchvision/transforms/v2/functional/_misc.html
                # for a hint (the part where it computes the padding).
                brights = torchvision.transforms.GaussianBlur((21, 1), sigma=7.0)(brights)  # blur along x
                brights = torchvision.transforms.GaussianBlur((1, 21), sigma=7.0)(brights)  # blur along y

                # Additively blend the images. Note we are working in linear intensity space, and we will now go over 1.0 intensity.
                image.add_(brights)

                # We now have a fake HDR image. Tonemap it back to LDR.
                image[:3, :, :] = 1.0 - torch.exp(-image[:3, :, :] * hdr_exposure)  # RGB: tonemap
                image[3, :, :] = torch.maximum(image[3, :, :], brights[3, :, :])  # alpha: max-combine
                torch.clamp_(image, min=0.0, max=1.0)

            def apply_scanlines(image: torch.tensor, field: int = 0, dynamic: bool = True) -> None:
                """CRT TV like scanlines.

                `field`: Which CRT field is dimmed at the first frame. 0 = top, 1 = bottom.
                `dynamic`: If `True`, the dimmed field will alternate each frame (top, bottom, top, bottom, ...)
                           for a more authentic CRT look (like Phosphor deinterlacer in VLC).
                """
                if dynamic:
                    start = (field + self.frame_no) % 2
                else:
                    start = field
                # We should ideally modify just the Y channel in YUV space, but modifying the alpha instead looks alright.
                image[3, start::2, :].mul_(0.5)
                self.frame_no += 1

            def apply_alphanoise(image: torch.tensor, magnitude: float = 0.1) -> None:
                """Dynamic noise to alpha channel. A cheap alternative to luma noise."""
                # TODO: add a feature to blur the noise, to control its spatial frequency ("size").
                base_magnitude = 1.0 - magnitude
                image[3, :, :].mul_(base_magnitude + magnitude * torch.rand(h, w, device=self.device))

            def apply_translucency(image: torch.tensor, alpha: float = 0.9) -> None:
                """A simple translucency filter for a hologram look.

                Multiplicatively adjusts the alpha channel.
                """
                image[3, :, :].mul_(alpha)

            def apply_banding(image: torch.tensor, strength: float = 0.4, density: float = 2.0, speed: float = 16.0) -> None:
                """Bad analog video signal, with traveling brighter and darker bands.

                This simulates a CRT display as it looks when filmed on video without syncing.

                `strength`: maximum brightness factor
                `density`: how many banding cycles per full image height
                `speed`: band movement, in pixels per frame
                """
                IMAGE_HEIGHT = self.poser.get_image_size()
                y = torch.linspace(0, math.pi, IMAGE_HEIGHT, dtype=self.poser.get_dtype(), device=self.device)

                # Animation
                cycle_pos = (self.frame_no / IMAGE_HEIGHT) * speed
                cycle_pos = cycle_pos - float(int(cycle_pos))  # fractional part
                cycle_pos = 1.0 - cycle_pos  # -> motion from top toward bottom

                band_effect = torch.sin(density * y + cycle_pos * math.pi)**2  # [512] = [h]
                band_effect = torch.unsqueeze(band_effect, 0)  # -> [1, 512] = [c, h]
                band_effect = torch.unsqueeze(band_effect, 2)  # -> [1, 512, 1] = [c, h, w]
                image[:3, :, :].mul_(1.0 + strength * band_effect)
                torch.clamp_(image, min=0.0, max=1.0)

            def apply_analog_lowres(image: torch.tensor, kernel_size: int = 5, sigma: float = 1.0) -> None:
                """Low-resolution analog video signal, simulated by blurring.

                `kernel_size`: size of the Gaussian blur kernel, in pixels.
                `sigma`: standard deviation of the Gaussian blur kernel, in pixels.

                Ideally, `kernel_size` should be `2 * (3 * sigma) + 1`, so that the kernel
                reaches its "3 sigma" (99.7% mass) point where the finitely sized kernel
                cuts the tail. "2 sigma" (95% mass) is also acceptable, to save some compute.

                The default settings create a slight blur without destroying much detail.
                """
                image[:, :, :] = torchvision.transforms.GaussianBlur((kernel_size, kernel_size), sigma=sigma)(image)

            def apply_analog_badhsync(image: torch.tensor, speed: float = 8.0,
                                      amplitude1: float = 0.001, density1: float = 4.0,
                                      amplitude2: Optional[float] = 0.001, density2: Optional[float] = 13.0,
                                      amplitude3: Optional[float] = 0.001, density3: Optional[float] = 27.0) -> None:
                """Analog video signal with fluctuating hsync.

                We superpose three waves with different densities (1 / cycle length)
                to make the pattern look more irregular.

                E.g. density of 2.0 means that two full waves fit into the image height.

                Amplitudes are given in units where the height and width of the image
                are both 2.0.
                """
                IMAGE_HEIGHT = self.poser.get_image_size()

                # Seems the deformation geometry must be float32 no matter the image data type.
                d = torch.linspace(-1.0, 1.0, IMAGE_HEIGHT, dtype=torch.float32, device=self.device)
                yy = d
                xx = d
                meshy, meshx = torch.meshgrid((yy, xx), indexing="ij")

                # Animation
                cycle_pos = (self.frame_no / IMAGE_HEIGHT) * speed
                cycle_pos = cycle_pos - float(int(cycle_pos))  # fractional part
                cycle_pos = 1.0 - cycle_pos  # -> motion from top toward bottom
                cycle_pos *= 2.0  # full cycle = 2 units

                # Deformation
                meshx = meshx + amplitude1 * torch.sin((density1 * (meshy + cycle_pos)) * math.pi)
                if amplitude2 and density2:
                    meshx = meshx + amplitude2 * torch.sin((density2 * (meshy + cycle_pos)) * math.pi)
                if amplitude3 and density3:
                    meshx = meshx + amplitude3 * torch.sin((density3 * (meshy + cycle_pos)) * math.pi)

                grid = torch.stack((meshx, meshy), 2)
                grid = grid.unsqueeze(0)  # batch of one
                image_batch = image.unsqueeze(0)  # batch of one -> [1, c, h, w]
                warped = torch.nn.functional.grid_sample(image_batch, grid, mode="bilinear", padding_mode="border", align_corners=False)
                warped = warped.squeeze(0)  # [1, c, h, w] -> [c, h, w]
                image[:, :, :] = warped

            def apply_analog_vhstracking(image: torch.tensor, base_offset: float = 0.03, max_dynamic_offset: float = 0.01, speed: float = 2.5) -> None:
                """1980s VHS tape with bad tracking.

                Image floats up and down, and a band of black and white noise appears at the bottom.

                Units like in `apply_analog_badhsync`.
                """
                IMAGE_HEIGHT = self.poser.get_image_size()

                # Seems the deformation geometry must be float32 no matter the image data type.
                d = torch.linspace(-1.0, 1.0, IMAGE_HEIGHT, dtype=torch.float32, device=self.device)
                yy = d
                xx = d
                meshy, meshx = torch.meshgrid((yy, xx), indexing="ij")

                # Animation
                cycle_pos = (self.frame_no / IMAGE_HEIGHT) * speed
                cycle_pos = cycle_pos - float(int(cycle_pos))  # fractional part
                cycle_pos *= 2.0  # full cycle = 2 units

                # Deformation - move image up/down
                yoffs = max_dynamic_offset * math.sin(cycle_pos * math.pi)
                meshy = meshy + yoffs

                grid = torch.stack((meshx, meshy), 2)
                grid = grid.unsqueeze(0)  # batch of one
                image_batch = image.unsqueeze(0)  # batch of one -> [1, c, h, w]
                warped = torch.nn.functional.grid_sample(image_batch, grid, mode="bilinear", padding_mode="border", align_corners=False)
                warped = warped.squeeze(0)  # [1, c, h, w] -> [c, h, w]
                image[:, :, :] = warped

                # Noise from bad VHS tracking at bottom
                yoffs_pixels = int((yoffs / 2.0) * 512.0)
                base_offset_pixels = int((base_offset / 2.0) * 512.0)
                noise_pixels = yoffs_pixels + base_offset_pixels
                if noise_pixels > 0:
                    image[:, -noise_pixels:, :] = _vhs_noise(height=noise_pixels)
                    # # Fade out toward left/right, since the character does not take up the full width.
                    # # Works, but fails at reaching the iconic VHS look.
                    # x = torch.linspace(0, math.pi, IMAGE_HEIGHT, dtype=self.poser.get_dtype(), device=self.device)
                    # fade = torch.sin(x)**2  # [w]
                    # fade = fade.unsqueeze(0)  # [1, w]
                    # image[3, -noise_pixels:, :] = fade

            def apply_analog_vhsglitches(image: torch.tensor, strength: float = 0.1, unboost: float = 4.0,
                                         max_glitches: int = 3, min_glitch_height: int = 3, max_glitch_height: int = 6) -> None:
                """Damaged 1980s VHS video tape, with transient (per-frame) glitching lines.

                This leaves the alpha channel alone, so the effect only affects parts that already show something.
                This is an artistic interpretation that makes the effect less distracting when used with RGBA data.

                `strength`: How much to blend in noise.
                `unboost`: Use this to adjust the probability profile for the appearance of glitches.
                           The higher `unboost` is, the less probable it is for glitches to appear at all,
                           and there will be fewer of them (in the same video frame) when they do appear.
                `max_glitches`: Maximum number of glitches in the video frame.
                `min_glitch_height`, `max_glitch_height`: in pixels. The height is randomized separately for each glitch.
                """
                IMAGE_HEIGHT = self.poser.get_image_size()
                n_glitches = torch.rand(1, device="cpu")**unboost  # higher probability of having none or few glitching lines
                n_glitches = int(max_glitches * n_glitches[0])
                if not n_glitches:
                    return
                glitch_start_lines = torch.rand(n_glitches, device="cpu")
                glitch_start_lines = [int((IMAGE_HEIGHT - (max_glitch_height - 1)) * x) for x in glitch_start_lines]
                for line in glitch_start_lines:
                    glitch_height = torch.rand(1, device="cpu")
                    glitch_height = int(min_glitch_height + (max_glitch_height - min_glitch_height) * glitch_height[0])
                    noise_image = _vhs_noise(height=glitch_height)
                    # Apply glitch to RGB only, so fully transparent parts stay transparent (important to make the effect less distracting).
                    image[:3, line:(line + glitch_height), :] = (1.0 - strength) * image[:3, line:(line + glitch_height), :] + strength * noise_image

            def _vhs_noise(height: int) -> torch.tensor:
                """Generate a band of noise that looks as if playing a blank VHS tape."""
                # This looks best if we randomize the alpha channel, too.
                noise_image = torch.rand(height, w, device=self.device, dtype=self.poser.get_dtype()).unsqueeze(0)  # [1, h, w]
                # Real VHS noise has horizontal runs of the same color, and the transitions between black and white are smooth.
                noise_image = torchvision.transforms.GaussianBlur((5, 1), sigma=2.0)(noise_image)
                return noise_image

            def apply_chromatic_aberration(image: torch.tensor, transverse_sigma: float = 0.5, axial_scale: float = 0.005) -> None:
                """Simulate the two types of chromatic aberration in a camera lens.

                Like everything else here, this is of course made of smoke and mirrors. We simulate the axial effect
                (index of refraction varying w.r.t. wavelength) by geometrically scaling the RGB channels individually,
                and the transverse effect (focal distance varying w.r.t. wavelength) by a gaussian blur.

                Note that in a real lens:
                  - Axial CA is typical at long focal lengths (e.g. tele/zoom lens)
                  - Axial CA increases at high F-stops (low depth of field, i.e. sharp focus at all distances)
                  - Transverse CA is typical at short focal lengths (e.g. macro lens)

                However, in an RGB postproc effect, it is useful to apply both together, to help hide the clear-cut red/blue bands
                resulting from the different geometric scalings of just three wavelengths (instead of a continuous spectrum, like
                a scene lit with natural light would have).

                See:
                    https://en.wikipedia.org/wiki/Chromatic_aberration
                """
                IMAGE_HEIGHT = self.poser.get_image_size()

                d = torch.linspace(-1.0, 1.0, IMAGE_HEIGHT, dtype=torch.float32, device=self.device)
                yy = d
                xx = d
                meshy, meshx = torch.meshgrid((yy, xx), indexing="ij")

                # Axial: Shrink R (deflected less), pass G through (lens reference wavelength), enlarge B (deflected more).
                grid_R = torch.stack((meshx * (1.0 + axial_scale), meshy * (1.0 + axial_scale)), 2)
                grid_R = grid_R.unsqueeze(0)
                grid_B = torch.stack((meshx * (1.0 - axial_scale), meshy * (1.0 - axial_scale)), 2)
                grid_B = grid_B.unsqueeze(0)

                image_batch_R = image[0, :, :].unsqueeze(0).unsqueeze(0)  # [h, w] -> [c, h, w] -> [n, c, h, w]
                warped_R = torch.nn.functional.grid_sample(image_batch_R, grid_R, mode="bilinear", padding_mode="border", align_corners=False)
                warped_R = warped_R.squeeze(0)  # [1, c, h, w] -> [c, h, w]
                image_batch_B = image[2, :, :].unsqueeze(0).unsqueeze(0)
                warped_B = torch.nn.functional.grid_sample(image_batch_B, grid_B, mode="bilinear", padding_mode="border", align_corners=False)
                warped_B = warped_B.squeeze(0)  # [1, c, h, w] -> [c, h, w]

                # Transverse (blur to simulate wrong focal distance for R and B)
                warped_R[:, :, :] = torchvision.transforms.GaussianBlur((5, 5), sigma=transverse_sigma)(warped_R)
                warped_B[:, :, :] = torchvision.transforms.GaussianBlur((5, 5), sigma=transverse_sigma)(warped_B)

                # Alpha channel: treat similarly to each of R,G,B and average the three resulting alpha channels
                image_batch_A = image[3, :, :].unsqueeze(0).unsqueeze(0)
                warped_A1 = torch.nn.functional.grid_sample(image_batch_A, grid_R, mode="bilinear", padding_mode="border", align_corners=False)
                warped_A1[:, :, :] = torchvision.transforms.GaussianBlur((5, 5), sigma=transverse_sigma)(warped_A1)
                warped_A2 = torch.nn.functional.grid_sample(image_batch_A, grid_B, mode="bilinear", padding_mode="border", align_corners=False)
                warped_A2[:, :, :] = torchvision.transforms.GaussianBlur((5, 5), sigma=transverse_sigma)(warped_A2)
                averaged_alpha = (warped_A1 + image[3, :, :] + warped_A2) / 3.0

                image[0, :, :] = warped_R
                # image[1, :, :] passed through as-is
                image[2, :, :] = warped_B
                image[3, :, :] = averaged_alpha

            def apply_vignetting(image: torch.tensor, strength: float = 0.42) -> None:
                """Simulate vignetting (less light hitting the corners of a film frame or CCD sensor).

                The profile used here is [cos(strength * d * pi)]**2, where `d` is the distance
                from the center, scaled such that `d = 1.0` is reached at the corners.
                Thus, at the midpoints of the frame edges, `d = 1 / sqrt(2) ~ 0.707`.
                """
                IMAGE_HEIGHT = self.poser.get_image_size()

                d = torch.linspace(-1.0, 1.0, IMAGE_HEIGHT, dtype=torch.float32, device=self.device)
                yy = d
                xx = d
                meshy, meshx = torch.meshgrid((yy, xx), indexing="ij")

                euclidean_distance_from_center = (meshy**2 + meshx**2)**0.5 / 2**0.5  # [h, w]

                brightness = torch.cos(strength * euclidean_distance_from_center * math.pi)**2  # [h, w]
                brightness = torch.unsqueeze(brightness, 0)  # -> [1, h, w]
                image[:3, :, :] *= brightness

            # apply postprocess chain (this is the correct order for the filters)

            # physical input signal
            apply_bloom(output_image)  # fake HDR; only makes sense with dark-ish backgrounds!

            # video camera
            apply_chromatic_aberration(output_image)
            apply_vignetting(output_image)

            # scifi hologram
            apply_translucency(output_image)
            apply_alphanoise(output_image)

            # # lo-fi analog video transport
            # apply_analog_lowres(output_image)
            # apply_analog_badhsync(output_image)
            # apply_analog_vhsglitches(output_image)
            # apply_analog_vhstracking(output_image)

            # CRT TV output
            apply_banding(output_image)
            apply_scanlines(output_image)

            # end postproc filters
            # --------------------------------------------------------------------------------

            output_image = convert_linear_to_srgb(output_image)  # apply gamma correction

            # convert [c, h, w] float -> [h, w, c] uint8
            output_image = torch.transpose(output_image.reshape(c, h * w), 0, 1).reshape(h, w, c)
            output_image = (255.0 * output_image).byte()

            output_image_numpy = output_image.detach().cpu().numpy()

        # Update FPS counter, measuring animation frame render time only.
        #
        # This says how fast the renderer *can* run on the current hardware;
        # note we don't actually render more frames than the client consumes.
        time_now = time.time_ns()
        if self.source_image is not None:
            elapsed_time = time_now - time_render_start
            fps = 1.0 / (elapsed_time / 10**9)
            self.fps_statistics.add_fps(fps)

        # Set the new rendered frame as the output image, and mark the frame as ready for consumption.
        with _animator_output_lock:
            self.result_image = output_image_numpy
            self.frame_ready = True

        # Log the FPS counter in 5-second intervals.
        if self.last_report_time is None or time_now - self.last_report_time > 5e9:
            trimmed_fps = round(self.fps_statistics.get_average_fps(), 1)
            logger.info("available render FPS: {:.1f}".format(trimmed_fps))
            self.last_report_time = time_now
