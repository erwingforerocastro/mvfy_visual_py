import logging
import uuid
from datetime import datetime
from time import sleep
from typing import Any, Optional

import cv2
import numpy as np
from apscheduler.triggers.cron import CronTrigger
from pydantic import Field
from data_access.visual_knowledge_db import SystemDB, UserDB
from mvfy.visual import func
from mvfy.visual.detector import Detector
from mvfy.visual.generator.image_generator import ImageGenerator
from mvfy.visual.receiver.receivers import Receiver
from mvfy.visual.streamer import Streamer
from dataclasses import dataclass
from tzlocal import get_localzone
from utils import constants as const
from utils import feature_flags as ft
from utils import index as utils

from . import errors


@dataclass
class VisualKnowledge(ImageGenerator):

    detector: Optional[Detector] = None 
    receiver: Optional[Receiver] = None
    type_service: Optional[str] = None
    db_properties: Optional[dict] = None
    db_name: Optional[str] = None
    max_descriptor_distance: Optional[float] = None
    min_date_knowledge: Optional[float] = None
    min_frequency: Optional[float] = 0.7
    resize_factor: Optional[float] = 0.25
    features: Optional[list] = Field(default_factory = list)
    type_system: Optional[str] = const.TYPE_SYSTEM["OPTIMIZED"]
    title: Optional[str] = str(uuid.uuid4())

    """
    Main model builder

    constructor
    :Parameters:
        {String} type_service - type of the listen server.
        {Dict} db - proprties of bd see https://pymongo.readthedocs.io/en/stable/api/pymongo/mongo_client.html#pymongo.mongo_client.MongoClient.
        {String} db_name - name of db to be used.
        {Array} min_date_knowledge [min_date_knowledge=null] - minimum interval to determine a known user.
        {Number} min_frequency [min_frequency=0.7] - minimum frequency between days detectioned.
        {list} features [features=null] - characteristics that will be saved in each detection.
        {String} max_descriptor_distance [max_descriptor_distance=null] - max distance of diference between detections.
        {String} type_system [type_system=null] - type of system.
        {String} title [title=null] - title of system.

    :Returns:
        None.

    """
    def __post_init__(self):

        #properties
        self.id = None

        #agents 
        self.streamer = None
        self.stream_fps = 30

        #more info
        self.type_model_detection = None
        self.display_size = (720, 720)
        self.matches = None
        self.interval_streaming = None
        self.execution = False
        self.date_format = const.DATE_FORMAT
        self.draw_label = True
        self.cron_reload = self.__get_cron_trigger()

        #DB
        self.db_systems = SystemDB(
            properties = self.db_properties,
            db = self.db_name,
            collection = const.COLLECTIONS["SYSTEMS"]
        )
        self.db_users = UserDB(
            properties = self.db_properties,
            db = self.db_name,
            collection = const.COLLECTIONS["USERS"]
        )
    
    def __aiter__(self) -> Any:

      return self
    
    async def __anext__(self) -> np.array:

        if self.images_queue.empty():
            await self.put_wait_image()
        
        image: np.array = await self.images_queue.get()

        yield image

    async def __preload(self) -> None:
        """
            Load system and users if exist

            Search the system or saved it
            Search the users of the system
        """
        # found or add system
        system = await func.get_system(self.get_obj(), self.db_systems)
        if system is None:
            system = await func.insert_system(self.get_obj(), self.db_systems)
            if system is None:
                raise errors.SystemNotFoundError(str(self.db_systems))

        #insert system found in instance
        self.__insert_system(system)

        #get descriptors
        users_queue = await func.load_user_descriptors(
            system_id = self.id,
            db = self.db_users
        )

        #load descriptors
        self.detector.authors =  users_queue

    def __get_cron_trigger(self) -> CronTrigger:
        """get crontrigger of now every day

        Returns:
            CronTrigger: trigger of action
        """
        _date = datetime.now(tz=get_localzone())
        return CronTrigger(hour=_date.hour, minute=_date.minute, second=_date.second, timezone=get_localzone())

    def __insert_system(self, system: dict) -> None:
        """Insert system inside actual instance

        Args:
            system (dict): values to be replaced in instance
        """
        self.id = system["id"]
        self.type_service = system["type_service"] if system["type_service"] is not None else self.type_service
        self.title = system["title"] if system["title"] is not None else self.title
        self.features = system["features"] if system["features"] is not None else self.features
        self.min_date_knowledge = system["min_date_knowledge"] if system["min_date_knowledge"] is not None else self.min_date_knowledge
        self.min_frequency = system["min_frequency"] if system["min_frequency"] is not None else self.min_frequency
        self.max_descriptor_distance = system["max_descriptor_distance"] if system["max_descriptor_distance"] is not None else self.max_descriptor_distance
        self.type_system = system["type_system"] if system["type_system"] is not None else self.type_system
        self.resize_factor = system["resize_factor"] if system["resize_factor"] is not None else self.resize_factor

    def set_conf(self, 
    display_size: Optional[dict] = None,
    date_format: Optional[str] = None,
    draw_label: Optional[bool] = None,
    cron_reload: Optional[CronTrigger] = None,
    ) -> None:
        """Set configuration parameters for this instance .

        Args:
            display_size (dict, optional): size of image process. Defaults to None.
        """

        #more info
        self.display_size = display_size if display_size is not None else self.display_size
        self.date_format = date_format if date_format is not None else self.date_format
        self.draw_label = draw_label if draw_label is not None else self.draw_label
        self.cron_reload = cron_reload if cron_reload is not None else self.cron_reload

    def get_obj(self) -> dict:
        """Get a dict of the attributes for this instance.

        Returns:
            dict: instance relevant features
        """
        return {
            "title": self.title,
            "type_service": self.type_service,
            "max_descriptor_distance": self.max_descriptor_distance,
            "min_date_knowledge": self.min_date_knowledge,
            "min_frequency": self.min_frequency,
            "features": self.features,
            "type_system": self.type_system,
            "resize_factor": self.resize_factor,
            "id": self.id,
        }

    async def start(self) -> None:
        
        await func.async_scheduler(job = self.__preload, trigger = self.cron_reload)
        
        while True:

            _, img = next(self.receiver)
            if img is not None:
                img_processed = await self.process_unknows(
                    img = img,
                    resize_factor = self.resize_factor,
                    draw_label = self.draw_label
                    )
            
            if ft.ENVIROMENT == "DEV":
                sleep(5)

            await self.images_queue.put(img_processed)

    async def process_unknows(self, img: np.array, resize_factor: float = 0.25, draw_label: bool = False) -> 'np.array':
        """Process unknowns users.

        Args:
            img (np.array): img with faces
            resize_factor (float, optional): resize image to acelerate process. Defaults to 0.25.
            draw_label (bool, optional): draw labels over the faces. Defaults to False.

        Returns:
            np.array: img
        """
        
        more_similar, less_similar = await self.detector.detect(img)

        # Display the results
        return_size = 1 / resize_factor

        for detection in less_similar:
                
            (top, right, bottom, left) = detection["location"]
            # Scale back up face locations since the frame we detected in was scaled to 1/4 size
            top *= return_size
            right *= return_size
            bottom *= return_size
            left *= return_size

            # Draw a box around the face
            cv2.rectangle(img, (left, top), (right, bottom), (0, 0, 255), 2)

            if draw_label is True:
                # Draw a label with a name below the face
                cv2.rectangle(img, (left, bottom - 35), (right, bottom), (0, 0, 255), cv2.FILLED)
                font = cv2.FONT_HERSHEY_DUPLEX
                cv2.putText(img, detection["name"], (left + 6, bottom - 6), font, 1.0, (255, 255, 255), 1)
        
            #save information of detection
            await self.set_detection({
                "system_id": self.id,
                "detection": detection["encoding"],
                "features": detection["features"],
                "author": str(uuid.uuid4()),
                "init_date": utils.get_actual_date(self.date_format),
                "last_date": utils.get_actual_date(self.date_format),
                "knowledge": False,
                "frequency": 0,
            })
        
        #evaluate knowed users
        for detection in more_similar:

            user = await func.find_user({
                "author": detection["author"]
            })
            if user is None:
                logging.error(f"Error user not found in BD, author: {detection['author']}")

            await self.evaluate_detection(user)

        return img

    async def evaluate_detection(self, user: dict) -> dict:
        """Evaluate the user s detection.

        Args:
            user (dict): user to be evaluated

        Returns:
            dict: user modified
        """
        prev_user = user
        diff_date = utils.get_date_diff_so_far(user.init_date, self.min_date_knowledge[1])

        if diff_date > self.min_date_knowledge[0] and user.frequency >= self.frequency:
            user["knowledge"] = True

        elif utils.get_date_diff_so_far(user.last_date, self.min_date_knowledge[1]) > 0:

            prev_days = utils.frequency(total = self.min_date_knowledge[0], percentage = 1, value = self.frequency, invert = True) 
            user.last_date = utils.get_actual_date(self.date_format)
            user.frequency = utils.frequency(self.min_date_knowledge[0], 1, prev_days + 1) 

        return await func.update_user({
            **user,
            "modified_on": utils.get_actual_date(self.date_format)
        }, self.db_users) if prev_user != user else user

    async def set_detection(self, user: dict) -> None:
        """Set the detection of a user in the database .

        Args:
            user (dict): [description]
        """
        try:    
            await func.insert_user(user, self.db_users)
        except Exception as e:
            logging.error(f"set_detection error to insert detection, {e}")