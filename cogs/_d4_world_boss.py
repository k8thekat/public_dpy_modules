
import pytz
from pathlib import Path
from datetime import datetime, timedelta
import json
from typing_extensions import Union, Any, Self
import copy


class WorldBoss():
    def __init__(self, boss_index: int = 1,
                 world_boss: Union[Self, None] = None,
                 spawn_pattern_index: int = 13,
                 times_spawned: int = 0,
                 time_pattern_index: int = 3,
                 time_offset: int = 1,
                 last_spawn: Union[None, datetime] = None,
                 tz_info: Any = pytz.timezone("US/Pacific")) -> None:
        """
        Args:
            boss_index (int, optional): Which boss entry we are at in the `_bosses` array. Defaults to 0.
            spawn_pattern_index (int, optional): Which spawn pattern we are at in the`_spawn_pattern` array. Defaults to 0.
            times_spawned (int, optional): How many times the boss has spawned at current iteration. Defaults to 1.
            time_offset (int, optional): _description_. Which time offset to use from our `_time_offsets` array to 0.
            time_pattern_index (int, optional): Which position in the `_time_pattern`we are at. Defaults to 0.
            last_spawn (Union[None, datetime], optional): Specific date of last known boss timer **MUST BE `pytz.timezone("US/Pacific")`**. Defaults to None.
            tz_info (pytz) : Specify the timezone to be used for calculating the spawns. **ONLY SUPPORTS "US/Pacific" ATM**
                _time_window's list would need to be adjusted for any offsets of timezone changes.
        """
        self._file_dir = Path(__file__).parent
        self._json: Path = self._file_dir.joinpath("d4timer.json")

        if isinstance(world_boss, WorldBoss):
            for attr in vars(world_boss):
                value = getattr(self, attr)
                setattr(self, attr, value)

        # **World Boss Timers**
        # https://www.reddit.com/r/diablo4/comments/14cv0t0/world_bosses_helltides_and_legion_events_are_all/
        # Last known spawn time
        self._tz_info = tz_info

        self._last_known_spawn: datetime = datetime(year=2023, month=10, day=2, hour=4, minute=41, tzinfo=self._tz_info)  # _time_pattern_index = 0, _time_offset_index= 1 , _boss_index= 0, _spawn_pattern_index = 0, _times_spawned = 1 of 3
        if last_spawn is not None:
            self._last_known_spawn = last_spawn

        self._last_boss: Union[None, dict[str, Any]] = None
        self._next_boss: Union[None, dict[str, Any]] = None

        self._boss_index = boss_index
        self._bosses: list[str] = [
            "Ashava",
            "Wandering Death",
            "Avarice"
        ]
        # Spawn Pattern  3x-2x-3x-2x (3x spawns of one boss; then 2x of another boss then repeating.(Rumor says 6 sets followed by (3-2-2) pattern))
        # This is JUST for bosses; Name's could be wrong until further testing.
        self._spawn_pattern: list[int] = [3, 2, 3, 2, 3, 2, 3, 2, 3, 2, 3, 2, 3, 2, 2]  # len = 15
        self._spawn_pattern_index = spawn_pattern_index
        self._times_spawned = times_spawned  # 1 of X(_spawn_pattern[_spawn_pattern_index])

        # PST
        # 10/2/2023 4:41:00 AM - Wandering Death - 325.? (Ashava?)      (1)
        # 10/2/2023 10:07:00 AM - Wandering Death - 353.?                   (1)
        # 10/2/2023 4:01:00 PM - Wandering Death - 353.29?                 (2)
        # 10/2/2023 9:53:58 PM - Wandering Death - 325.13? (Avarice?)  (1)
        # 10/3/2023 5:19:11 AM - Wandering Death - 353.29 (Avarice?)   (2)
        # 10/3/2023 11:12:41 AM - Avarice -325.13 (Ashava?)                 (1)
        # 10/3/2023 4:37:54 PM - Ashava - 353.29                                  (2)
        # 10/3/2023 10:31:24 PM - Ashava? -

        # These specify how many times to repeat the time offset.
        self._time_pattern_index: int = time_pattern_index
        self._time_pattern: list[int] = [2, 1, 1, 1]
        self._time_used: int = 0

        # These specific how far till the next spawn from current time.
        self._time_offset_index = time_offset
        self._time_offsets: list[timedelta] = [
            timedelta(minutes=353, seconds=29),  # 29
            timedelta(minutes=325, seconds=13)]  # 13

        # Time Windows (+2hrs if not inside these windows)(TZinfo = US/Pacific (UTC-8))
        # TODO - Possible allow a user to pass in a UTC offset and dynamically adjust the time windows?
        self._time_window: list[tuple[timedelta, timedelta]] = [
            (timedelta(hours=21, minutes=30), timedelta(hours=23, minutes=30)),  # 00:30 - 02:30
            (timedelta(hours=3, minutes=30), timedelta(hours=5, minutes=30)),  # 06:30 - 08:30
            (timedelta(hours=9, minutes=30), timedelta(hours=11, minutes=30)),  # 12:30 - 14:30
            (timedelta(hours=15, minutes=30), timedelta(hours=17, minutes=30))]  # 18:30 - 20:30

    @property
    def last_boss(self) -> str | None:
        if self._last_boss is None:
            return
        if isinstance(self._last_boss['time'], datetime):
            _time: str = self._last_boss['time'].strftime('%B, %d - %H:%M:%S')
            return f"Last World Boss Spawn: {self._last_boss['name']} @ {_time} (+/- 1 minute)"

    @property
    def next_boss(self) -> str | None:
        if self._next_boss is None:
            return
        if isinstance(self._next_boss['time'], datetime):
            _time: str = self._next_boss['time'].strftime('%B, %d - %H:%M:%S')
            return f"Next World Boss Spawn: {self._next_boss['name']} @ {_time} (+/- 1 minute)"

    @property
    def copy_boss(self) -> Self:
        return copy.deepcopy(self)

    def sequence_bosses(self, num: int) -> list:
        _future_bosses: list[dict[str, Union[str, datetime]]] = []
        _boss = WorldBoss(boss_index=self._boss_index, spawn_pattern_index=self._spawn_pattern_index, times_spawned=self._times_spawned, time_pattern_index=self._time_pattern_index, time_offset=self._time_offset_index, last_spawn=self._last_known_spawn)  # type:ignore
        time: datetime = self._last_known_spawn
        for x in range(0, num):
            if isinstance(time, datetime):
                time, boss = _boss.spawn_counter(last_time=time)
                _time = time.strftime('%B, %d - %H:%M:%S')
            _future_bosses.append({"name": boss, "time": f'{_time} (+/- 1 minute)'})
        return _future_bosses

    def time_checker(self, after: timedelta, before: timedelta, time: Union[timedelta, datetime]) -> bool:
        """
        Checks if our time's (hour, minutes) value lands in the window provided.

        Args:
            after (timedelta): The Hour, Minute value you want `time` to be after.
            before (timedelta): The Hour, Minute value you want `time` to be before.
            time (Union[timedelta, datetime]): Our Time value.

        Returns:
            bool: True if `time` is inbetween `after` and `before`
                else returns False.
        """
        if isinstance(time, datetime):
            c_time = timedelta(hours=time.hour, minutes=time.minute)
        else:
            c_time: timedelta = time
        if c_time >= after and c_time <= before:
            # print(f"The time value is within the window {after} - {before}")
            return True
        else:
            # print(f"We are outside our time window {after} - {before}")
            return False

    def next_spawn(self, boss_index: int, spawn_index: int, time_pattern: int, time_index: int, start_time: datetime) -> datetime:
        """
        Calculates the next possible boss spawn time. Used in conjunction with `time_checker`.

        Args:
            boss_index (int): Current position in `_bosses` array.
            spawn_index (int): Current position in `_spawn_pattern` array.
            time_index (int): Current position in `_time_offsets` array
            start_time (datetime): Usually a last known spawn datetime value; must be in `"US/Pacific"`

        Returns:
            datetime: Possible spawn time as datetime object.
        """
        if boss_index > (len(self._bosses) - 1):
            raise ValueError(f"It appears you have provided a boss index out of range 0 - {len(self._bosses)-1}")
        if spawn_index > (len(self._spawn_pattern) - 1):
            raise ValueError(f"It appears you have provided a spawn index out of range 0 - {len(self._spawn_pattern)-1}")
        if time_index > (len(self._time_offsets) - 1):
            raise ValueError(f"It appears you have provided a time index out of range 0 - {len(self._time_offsets)-1}")
        if time_pattern > (len(self._time_pattern) - 1):
            raise ValueError(f"It appears you have provided a time pattern out of range 0 - {len(self._time_pattern)-1}")
        offset: timedelta = self._time_offsets[time_index]
        spawn_time: datetime = start_time + offset
        return spawn_time

    def spawn_counter(self, last_time: datetime) -> tuple[datetime, str]:
        """
        Determines the next spawn time and if the time is in the correct window of spawn times.

        Args:
            last_time (datetime): Usually a last known spawn datetime value; must be in `"US/Pacific"`.

        Returns:
            datetime: Next boss to be spawned and the exact datetime they will spawn.
        """
        _spawn = False
        self._last_boss = {"name": self._bosses[self._boss_index - 1], "time": last_time}
        time: datetime = self.next_spawn(boss_index=self._boss_index, spawn_index=self._spawn_pattern_index, time_pattern=self._time_pattern_index, time_index=self._time_offset_index, start_time=last_time)
        val = timedelta(hours=time.hour, minutes=time.minute)
        for window in self._time_window:
            if self.time_checker(after=window[0], before=window[1], time=val):
                _spawn = True
                last_time = time
                self._times_spawned += 1
                self._time_used += 1
                break

        if _spawn == False:
            last_time += timedelta(hours=2)
            return self.spawn_counter(last_time=last_time)

        num_ofspawns: int = self._spawn_pattern[self._spawn_pattern_index] - self._times_spawned
        repeat_time: int = self._time_pattern[self._time_pattern_index] - self._time_used
        # print(f"Num of spawns left - {num_ofspawns} | Limit {self._spawn_pattern[self._spawn_pattern_index]}")
        # print(f"Num of time offset to be re-used - {repeat_time} | Limit {self._time_pattern[self._time_pattern_index]}")
        cur_boss: str = self._bosses[self._boss_index]
        if num_ofspawns == 0:
            self._times_spawned = 0

            self._boss_index = (self._boss_index + 1) % len(self._bosses)
            self._spawn_pattern_index = (self._spawn_pattern_index + 1) % len(self._spawn_pattern)

        if repeat_time == 0:  # or self._time_used == self._time_pattern[self._time_pattern_index]:
            self._time_used = 0
            self._time_pattern_index = (self._time_pattern_index + 1) % len(self._time_pattern)
            self._time_offset_index = (self._time_offset_index + 1) % len(self._time_offsets)

        # self._last_known_spawn = last_time
        # print(f"Next Boss Spawn: {cur_boss} @ {last_time.strftime('%B, %d - %I:%M:%S %p')} (+/- 1 minute)")
        # print(f"Next Boss Spawn: {cur_boss} @ {last_time.strftime('%B, %d - %H:%M:%S')} (+/- 1 minute)")
        self._next_boss = {"name": cur_boss, "time": last_time}
        return last_time, cur_boss

    def json_load(self) -> datetime:
        if self._json.is_file() is False:
            with open(self._json, "x") as jfile:
                jfile.close()
                self.json_save()
                return self._last_known_spawn

        else:
            with open(self._json, "r") as jfile:
                data = json.load(jfile)

        for setting in ["last_spawn", "offset", "boss", "spawn", "lives"]:
            if setting not in data:
                # print("Error loading setting", setting)
                return self._last_known_spawn

        self._last_known_spawn = datetime.fromtimestamp(int(data["last_spawn"]), tz=self._tz_info)
        self._time_offset_index = int(data["offset"])
        self._boss_index = int(data["boss"])
        self._time_pattern_index = int(data["pattern"])
        self._spawn_pattern_index = int(data["spawn"])
        self._times_spawned = int(data["lives"])
        return self._last_known_spawn

    def json_save(self) -> None:
        data: dict[str, int | float] = {
            "last_spawn": self._last_known_spawn.timestamp(),
            "offset": self._time_offset_index,
            "pattern": self._time_pattern_index,
            "boss": self._boss_index,
            "spawn": self._spawn_pattern_index,
            "lives": self._times_spawned
        }

        with open(self._json, "w") as jfile:
            json.dump(data, jfile)
            jfile.close()
