import datetime
from typing import AbstractSet, FrozenSet, Mapping, NamedTuple, Optional

import pendulum

import dagster._check as check
from dagster._annotations import experimental
from dagster._core.errors import DagsterInvalidDefinitionError
from dagster._serdes import whitelist_for_serdes
from dagster._utils.schedules import (
    cron_string_iterator,
    is_valid_cron_schedule,
    reverse_cron_string_iterator,
)

from .events import AssetKey


class FreshnessConstraint(NamedTuple):
    asset_keys: AbstractSet[AssetKey]
    required_data_time: datetime.datetime
    required_by_time: datetime.datetime


@experimental
@whitelist_for_serdes
class FreshnessPolicy(
    NamedTuple(
        "_FreshnessPolicy",
        [
            ("maximum_lag_minutes", float),
            ("cron_schedule", Optional[str]),
            ("cron_schedule_timezone", Optional[str]),
        ],
    )
):
    """
    A FreshnessPolicy specifies how up-to-date you want a given asset to be.

    Attaching a FreshnessPolicy to an asset definition encodes an expectation on the upstream data
    that you expect to be incorporated into the current state of that asset at certain points in time.
    How this is calculated differs depending on if the asset is unpartitioned or time-partitioned
    (other partitioning schemes are not supported).

    For time-partitioned assets, the current data time for the asset is simple to calculate. The
    upstream data that is incorporated into the asset is exactly the set of materialized partitions
    for that asset. Thus, the current data time for the asset is simply the time up to which all
    partitions have been materialized.

    For unpartitioned assets, the current data time is based on the upstream materialization records
    that were read to generate the current state of the asset. More specifically,
    imagine you have two assets, where A depends on B. If `B` has a FreshnessPolicy defined, this
    means that at time T, the most recent materialization of `B` should have come after a
    materialization of `A` which was no more than `maximum_lag_minutes` ago. This calculation is
    recursive: any given asset is expected to incorporate up-to-date data from all of its upstream
    assets.

    It is assumed that all asset definitions with no upstream asset definitions consume from some
    always-updating source. That is, if you materialize that asset at time T, it will incorporate
    all data up to time T.

    If `cron_schedule` is not defined, the given asset will be expected to incorporate upstream
    data from no more than `maximum_lag_minutes` ago at all points in time. For example, "The events
    table should always have data from at most 1 hour ago".

    If `cron_schedule` is defined, the given asset will be expected to incorporate upstream data
    from no more than `maximum_lag_minutes` ago at each cron schedule tick. For example, "By 9AM,
    the signups table should contain all of yesterday's data".

    The freshness status of assets with policies defined will be visible in the UI. If you are using
    an asset reconciliation sensor, this sensor will kick off runs to help keep your assets up to
    date with respect to their FreshnessPolicy.

    Args:
        maximum_lag_minutes (float): An upper bound for how old the data contained within this
            asset may be.
        cron_schedule (Optional[str]): A cron schedule string (e.g. ``"0 1 * * *"``) specifying a
            series of times by which the `maximum_lag_minutes` constraint must be satisfied. If
            no cron schedule is provided, then this constraint must be satisfied at all times.
        cron_schedule_timezone (Optional[str]): Timezone in which the cron schedule should be evaluated.
            If not specified, defaults to UTC. Supported strings for timezones are the ones provided
            by the `IANA time zone database <https://www.iana.org/time-zones>` - e.g.
            "America/Los_Angeles".

    .. code-block:: python

        # At any point in time, this asset must incorporate all upstream data from at least 30 minutes ago.
        @asset(freshness_policy=FreshnessPolicy(maximum_lag_minutes=30))
        def fresh_asset():
            ...

        # At any point in time, this asset must incorporate all upstream data from at least 30 minutes ago.
        @asset(freshness_policy=FreshnessPolicy(maximum_lag_minutes=30))
        def cron_up_to_date_asset():
            ...

    """

    def __new__(
        cls,
        *,
        maximum_lag_minutes: float,
        cron_schedule: Optional[str] = None,
        cron_schedule_timezone: Optional[str] = None,
    ):
        if cron_schedule is not None:
            if not is_valid_cron_schedule(cron_schedule):
                raise DagsterInvalidDefinitionError(f"Invalid cron schedule '{cron_schedule}'.")
            check.param_invariant(
                is_valid_cron_schedule(cron_schedule),
                "cron_schedule",
                f"Invalid cron schedule '{cron_schedule}'.",
            )
        if cron_schedule_timezone is not None:
            check.param_invariant(
                cron_schedule is not None,
                "cron_schedule_timezone",
                "Cannot specify cron_schedule_timezone without a cron_schedule.",
            )
            try:
                # Verify that the timezone can be loaded
                pendulum.tz.timezone(cron_schedule_timezone)  # type: ignore
            except Exception as e:
                raise DagsterInvalidDefinitionError(
                    "Invalid cron schedule timezone '{cron_schedule_timezone}'.   "
                ) from e
        return super(FreshnessPolicy, cls).__new__(
            cls,
            maximum_lag_minutes=float(
                check.numeric_param(maximum_lag_minutes, "maximum_lag_minutes")
            ),
            cron_schedule=check.opt_str_param(cron_schedule, "cron_schedule"),
            cron_schedule_timezone=check.opt_str_param(
                cron_schedule_timezone, "cron_schedule_timezone"
            ),
        )

    @classmethod
    def _create(cls, *args):
        """
        Pickle requires a method with positional arguments to construct
        instances of a class. Since the constructor for this class has
        keyword arguments only, we define this method to be used by pickle.
        """
        return cls(maximum_lag_minutes=args[0], cron_schedule=args[1])

    def __reduce__(self):
        return (self._create, (self.maximum_lag_minutes, self.cron_schedule))

    @property
    def maximum_lag_delta(self) -> datetime.timedelta:
        return datetime.timedelta(minutes=self.maximum_lag_minutes)

    def constraints_for_time_window(
        self,
        window_start: datetime.datetime,
        window_end: datetime.datetime,
        upstream_keys: FrozenSet[AssetKey],
    ) -> AbstractSet[FreshnessConstraint]:
        """For a given time window, calculate a set of FreshnessConstraints that this asset must
        satisfy.

        Args:
            window_start (datetime): The start time of the window that constraints will be
                calculated for. Generally, this is the current time.
            window_start (datetime): The end time of the window that constraints will be
                calculated for.
            upstream_keys (FrozenSet[AssetKey]): The relevant upstream keys for this policy.
        """
        constraints = set()

        # get an iterator of times to evaluate these constraints at
        if self.cron_schedule:
            constraint_ticks = cron_string_iterator(
                start_timestamp=window_start.timestamp(),
                cron_string=self.cron_schedule,
                execution_timezone=self.cron_schedule_timezone,
            )
        else:
            # this constraint must be satisfied at all points in time, so generate a series of
            # many constraints (10 per maximum lag window)
            period = pendulum.period(pendulum.instance(window_start), pendulum.instance(window_end))
            # old versions of pendulum return a list, so ensure this is an iterator
            constraint_ticks = iter(
                period.range("minutes", (self.maximum_lag_minutes / 10.0) + 0.1)
            )

        # iterate over each schedule tick in the provided time window
        evaluation_tick = next(constraint_ticks, None)
        while evaluation_tick is not None and evaluation_tick < window_end:
            required_data_time = evaluation_tick - self.maximum_lag_delta
            required_by_time = evaluation_tick

            constraints.add(
                FreshnessConstraint(
                    asset_keys=upstream_keys,
                    required_data_time=required_data_time,
                    required_by_time=required_by_time,
                )
            )

            evaluation_tick = next(constraint_ticks, None)
            # fallback if the user selects a very small maximum_lag_minutes value
            if len(constraints) > 100:
                break
        return constraints

    def minutes_late(
        self,
        evaluation_time: datetime.datetime,
        used_data_times: Mapping[AssetKey, Optional[datetime.datetime]],
    ) -> Optional[float]:
        """Returns a number of minutes past the specified freshness policy that this asset currently
        is. If the asset is missing upstream data, or is not materialized at all, then it is unknown
        how late it is, and this will return None.

        Args:
            evaluation_time (datetime): The time at which we're evaluating the lateness of this
                asset. Generally, this is the current time.
            used_data_times (Mapping[AssetKey, Optional[datetime]]): For each of the relevant
                upstream assets, the timestamp of the data that was used to create the current
                version of this asset.
        """
        if self.cron_schedule:
            # most recent cron schedule tick
            schedule_ticks = reverse_cron_string_iterator(
                end_timestamp=evaluation_time.timestamp(),
                cron_string=self.cron_schedule,
                execution_timezone=self.cron_schedule_timezone,
            )
            evaluation_tick = next(schedule_ticks)
        else:
            evaluation_tick = evaluation_time

        minutes_late = 0.0
        for used_data_time in used_data_times.values():
            # upstream data was not used, undefined how out of date you are
            if used_data_time is None:
                return None

            required_time = evaluation_tick - self.maximum_lag_delta
            if used_data_time < required_time:
                minutes_late = max(
                    minutes_late, (required_time - used_data_time).total_seconds() / 60
                )
        return minutes_late
