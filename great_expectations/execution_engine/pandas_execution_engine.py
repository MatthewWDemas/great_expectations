import datetime
import inspect
import json
import logging
import uuid
from functools import partial, wraps
from io import StringIO
from typing import List

import jsonschema
import numpy as np
import pandas as pd
from dateutil.parser import parse
from scipy import stats

from great_expectations.core import ExpectationConfiguration
from great_expectations.data_asset import DataAsset
from great_expectations.data_asset.util import DocInherit, parse_result_format
from great_expectations.dataset.util import (
    _scipy_distribution_positional_args_from_dict,
    is_valid_continuous_partition_object,
    validate_distribution_parameters,
)
from great_expectations.execution_environment.types import (PathBatchSpec, S3BatchSpec,)

from ..core.batch import Batch
from ..datasource.pandas_datasource import HASH_THRESHOLD
from ..exceptions import BatchKwargsError, BatchSpecError
from ..execution_environment.types import BatchMarkers
from ..execution_environment.util import S3Url, hash_pandas_dataframe
from .execution_engine import ExecutionEngine

logger = logging.getLogger(__name__)


class MetaPandasExecutionEngine(ExecutionEngine):
    """MetaPandasExecutionEngine is a thin layer between Dataset and PandasExecutionEngine.

    This two-layer inheritance is required to make @classmethod decorators work.

    Practically speaking, that means that MetaPandasExecutionEngine implements \
    expectation decorators, like `column_map_expectation` and `column_aggregate_expectation`, \
    and PandasExecutionEngine implements the expectation methods themselves.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @classmethod
    def column_map_expectation(cls, func):
        """Constructs an expectation using column-map semantics.


        The MetaPandasExecutionEngine implementation replaces the "column" parameter supplied by the user with a pandas
        Series
        object containing the actual column from the relevant pandas dataframe. This simplifies the implementing expectation
        logic while preserving the standard Dataset signature and expected behavior.

        See :func:`column_map_expectation <great_expectations.data_asset.dataset.Dataset.column_map_expectation>` \
        for full documentation of this function.
        """
        argspec = inspect.getfullargspec(func)[0][1:]

        @cls.expectation(argspec)
        @wraps(func)
        def inner_wrapper(
            self,
            column,
            mostly=None,
            result_format=None,
            row_condition=None,
            condition_parser=None,
            *args,
            **kwargs
        ):

            if result_format is None:
                result_format = self.default_expectation_args[
                    "result_format"
                ]  # TODO: should this be in batch_params?

            result_format = parse_result_format(result_format)
            if row_condition:
                if condition_parser not in ["python", "pandas"]:
                    raise ValueError(
                        "condition_parser is required when setting a row_condition,"
                        " and must be 'python' or 'pandas'"
                    )
                else:
                    data = self.dataframe.query(
                        row_condition, parser=condition_parser
                    ).reset_index(drop=True)
            else:
                data = self.dataframe

            series = data[column]
            if func.__name__ in [
                "expect_column_values_to_not_be_null",
                "expect_column_values_to_be_null",
            ]:
                # Counting the number of unexpected values can be expensive when there is a large
                # number of np.nan values.
                # This only happens on expect_column_values_to_not_be_null expectations.
                # Since there is no reason to look for most common unexpected values in this case,
                # we will instruct the result formatting method to skip this step.
                # FIXME rename to mapped_ignore_values?
                boolean_mapped_null_values = np.full(series.shape, False)
                result_format["partial_unexpected_count"] = 0
            else:
                boolean_mapped_null_values = series.isnull().values

            element_count = int(len(series))

            # FIXME rename nonnull to non_ignored?
            nonnull_values = series[boolean_mapped_null_values == False]
            nonnull_count = int((boolean_mapped_null_values == False).sum())

            boolean_mapped_success_values = func(self, nonnull_values, *args, **kwargs)
            success_count = np.count_nonzero(boolean_mapped_success_values)

            unexpected_list = list(
                nonnull_values[boolean_mapped_success_values == False]
            )
            unexpected_index_list = list(
                nonnull_values[boolean_mapped_success_values == False].index
            )

            if "output_strftime_format" in kwargs:
                output_strftime_format = kwargs["output_strftime_format"]
                parsed_unexpected_list = []
                for val in unexpected_list:
                    if val is None:
                        parsed_unexpected_list.append(val)
                    else:
                        if isinstance(val, str):
                            val = parse(val)
                        parsed_unexpected_list.append(
                            datetime.datetime.strftime(val, output_strftime_format)
                        )
                unexpected_list = parsed_unexpected_list

            success, percent_success = self._calc_map_expectation_success(
                success_count, nonnull_count, mostly
            )

            return_obj = self._format_map_output(
                result_format,
                success,
                element_count,
                nonnull_count,
                len(unexpected_list),
                unexpected_list,
                unexpected_index_list,
            )

            # FIXME Temp fix for result format
            if func.__name__ in [
                "expect_column_values_to_not_be_null",
                "expect_column_values_to_be_null",
            ]:
                del return_obj["result"]["unexpected_percent_nonmissing"]
                del return_obj["result"]["missing_count"]
                del return_obj["result"]["missing_percent"]
                try:
                    del return_obj["result"]["partial_unexpected_counts"]
                    del return_obj["result"]["partial_unexpected_list"]
                except KeyError:
                    pass

            return return_obj

        inner_wrapper.__name__ = func.__name__
        inner_wrapper.__doc__ = func.__doc__

        return inner_wrapper

    @classmethod
    def column_pair_map_expectation(cls, func):
        """
        The column_pair_map_expectation decorator handles boilerplate issues surrounding the common pattern of evaluating
        truthiness of some condition on a per row basis across a pair of columns.
        """
        argspec = inspect.getfullargspec(func)[0][1:]

        @cls.expectation(argspec)
        @wraps(func)
        def inner_wrapper(
            self,
            column_A,
            column_B,
            mostly=None,
            ignore_row_if="both_values_are_missing",
            result_format=None,
            row_condition=None,
            condition_parser=None,
            *args,
            **kwargs
        ):

            if result_format is None:
                result_format = self.default_expectation_args[
                    "result_format"
                ]  # TODO: should this be in batch_params?

            if row_condition:
                self = self.dataframe.query(row_condition).reset_index(drop=True)

            series_A = self[column_A]
            series_B = self[column_B]

            if ignore_row_if == "both_values_are_missing":
                boolean_mapped_null_values = series_A.isnull() & series_B.isnull()
            elif ignore_row_if == "either_value_is_missing":
                boolean_mapped_null_values = series_A.isnull() | series_B.isnull()
            elif ignore_row_if == "never":
                boolean_mapped_null_values = series_A.map(lambda x: False)
            else:
                raise ValueError("Unknown value of ignore_row_if: %s", (ignore_row_if,))

            assert len(series_A) == len(
                series_B
            ), "Series A and B must be the same length"

            # This next bit only works if series_A and _B are the same length
            element_count = int(len(series_A))
            nonnull_count = (boolean_mapped_null_values == False).sum()

            nonnull_values_A = series_A[boolean_mapped_null_values == False]
            nonnull_values_B = series_B[boolean_mapped_null_values == False]
            nonnull_values = [
                value_pair
                for value_pair in zip(list(nonnull_values_A), list(nonnull_values_B))
            ]

            boolean_mapped_success_values = func(
                self, nonnull_values_A, nonnull_values_B, *args, **kwargs
            )
            success_count = boolean_mapped_success_values.sum()

            unexpected_list = [
                value_pair
                for value_pair in zip(
                    list(
                        series_A[
                            (boolean_mapped_success_values == False)
                            & (boolean_mapped_null_values == False)
                        ]
                    ),
                    list(
                        series_B[
                            (boolean_mapped_success_values == False)
                            & (boolean_mapped_null_values == False)
                        ]
                    ),
                )
            ]
            unexpected_index_list = list(
                series_A[
                    (boolean_mapped_success_values == False)
                    & (boolean_mapped_null_values == False)
                ].index
            )

            success, percent_success = self._calc_map_expectation_success(
                success_count, nonnull_count, mostly
            )

            return_obj = self._format_map_output(
                result_format,
                success,
                element_count,
                nonnull_count,
                len(unexpected_list),
                unexpected_list,
                unexpected_index_list,
            )

            return return_obj

        inner_wrapper.__name__ = func.__name__
        inner_wrapper.__doc__ = func.__doc__
        return inner_wrapper

    @classmethod
    def multicolumn_map_expectation(cls, func):
        """
        The multicolumn_map_expectation decorator handles boilerplate issues surrounding the common pattern of
        evaluating truthiness of some condition on a per row basis across a set of columns.
        """
        argspec = inspect.getfullargspec(func)[0][1:]

        @cls.expectation(argspec)
        @wraps(func)
        def inner_wrapper(
            self,
            column_list,
            mostly=None,
            ignore_row_if="all_values_are_missing",
            result_format=None,
            row_condition=None,
            condition_parser=None,
            *args,
            **kwargs
        ):

            if result_format is None:
                result_format = self.default_expectation_args[
                    "result_format"
                ]  # TODO: should this be in batch_params?

            if row_condition:
                self = self.dataframe.query(row_condition).reset_index(drop=True)

            test_df = self[column_list]

            if ignore_row_if == "all_values_are_missing":
                boolean_mapped_skip_values = test_df.isnull().all(axis=1)
            elif ignore_row_if == "any_value_is_missing":
                boolean_mapped_skip_values = test_df.isnull().any(axis=1)
            elif ignore_row_if == "never":
                boolean_mapped_skip_values = pd.Series([False] * len(test_df))
            else:
                raise ValueError("Unknown value of ignore_row_if: %s", (ignore_row_if,))

            boolean_mapped_success_values = func(
                self, test_df[boolean_mapped_skip_values == False], *args, **kwargs
            )
            success_count = boolean_mapped_success_values.sum()
            nonnull_count = (~boolean_mapped_skip_values).sum()
            element_count = len(test_df)

            unexpected_list = test_df[
                (boolean_mapped_skip_values == False)
                & (boolean_mapped_success_values == False)
            ]
            unexpected_index_list = list(unexpected_list.index)

            success, percent_success = self._calc_map_expectation_success(
                success_count, nonnull_count, mostly
            )

            return_obj = self._format_map_output(
                result_format,
                success,
                element_count,
                nonnull_count,
                len(unexpected_list),
                unexpected_list.to_dict(orient="records"),
                unexpected_index_list,
            )

            return return_obj

        inner_wrapper.__name__ = func.__name__
        inner_wrapper.__doc__ = func.__doc__
        return inner_wrapper


class PandasExecutionEngine(MetaPandasExecutionEngine):
    """
PandasExecutionEngine instantiates the great_expectations Expectations API as a subclass of a pandas.DataFrame.

For the full API reference, please see :func:`Dataset <great_expectations.data_asset.dataset.Dataset>`

Notes:
    1. Samples and Subsets of PandaDataSet have ALL the expectations of the original \
       data frame unless the user specifies the ``discard_subset_failing_expectations = True`` \
       property on the original data frame.
    2. Concatenations, joins, and merges of PandaDataSets contain NO expectations (since no autoinspection
       is performed by default).

--ge-feature-maturity-info--

    id: validation_engine_pandas
    title: Validation Engine - Pandas
    icon:
    short_description: Use Pandas DataFrame to validate data
    description: Use Pandas DataFrame to validate data
    how_to_guide_url:
    maturity: Production
    maturity_details:
        api_stability: Stable
        implementation_completeness: Complete
        unit_test_coverage: Complete
        integration_infrastructure_test_coverage: N/A -> see relevant Datasource evaluation
        documentation_completeness: Complete
        bug_risk: Low
        expectation_completeness: Complete

--ge-feature-maturity-info--
    """

    # this is necessary to subclass pandas in a proper way.
    # NOTE: specifying added properties in this way means that they will NOT be carried over when
    # the dataframe is manipulated, which we might want. To specify properties that are carried over
    # to manipulation results, we would just use `_metadata = ['row_count', ...]` here. The most likely
    # case is that we want the former, but also want to re-initialize these values to None so we don't
    # get an attribute error when trying to access them (I think this could be done in __finalize__?)
    _internal_names = pd.DataFrame._internal_names + [
        "_batch_spec",
        "_batch_markers",
        "_batch_definition",
        "_batch_id",
        "_expectation_suite",
        "_config",
        "caching",
        "default_expectation_args",
        "discard_subset_failing_expectations",
    ]
    _internal_names_set = set(_internal_names)

    recognized_batch_definition_keys = {
        "limit"
    }

    recognized_batch_spec_defaults = {
        "reader_method",
        "reader_options",
    }

    # We may want to expand or alter support for subclassing dataframes in the future:
    # See http://pandas.pydata.org/pandas-docs/stable/extending.html#extending-subclassing-pandas

    @property
    def _constructor(self):
        return self.__class__

    def __finalize__(self, other, method=None, **kwargs):
        if isinstance(other, PandasExecutionEngine):
            self._initialize_expectations(other._expectation_suite)
            # If other was coerced to be a PandasExecutionEngine (e.g. via _constructor call during self.copy()
            # operation)
            # then it may not have discard_subset_failing_expectations set. Default to self value
            self.discard_subset_failing_expectations = getattr(
                other,
                "discard_subset_failing_expectations",
                self.discard_subset_failing_expectations,
            )
            if self.discard_subset_failing_expectations:
                self.discard_failing_expectations()
        super().__finalize__(other, method, **kwargs)
        return self

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.discard_subset_failing_expectations = kwargs.get(
            "discard_subset_failing_expectations", False
        )

    def load_batch(self, batch_definition, in_memory_dataset=None):
        execution_environment_name = batch_definition.get("execution_environment")
        execution_environment = self._data_context.get_execution_environment(
            execution_environment_name
        )

        # We need to build a batch_markers to be used in the dataframe
        batch_markers = BatchMarkers(
            {
                "ge_load_time": datetime.datetime.now(datetime.timezone.utc).strftime(
                    "%Y%m%dT%H%M%S.%fZ"
                )
            }
        )

        data_connector_name = batch_definition.get("data_connector")
        # TODO: Is it ok that this in_memory_dataset is a batch_definition key and not top level?
        if in_memory_dataset is not None:
            if batch_definition.get("data_asset_name") and batch_definition.get(
                "partition_id"
            ):
                df = in_memory_dataset
                batch_spec = {}
                batch_definition["data_connector"] = "dummy_data_connector"
            else:
                raise ValueError(
                    "To pass an in_memory_dataset, you must also pass a data_asset_name "
                    "and partition_id"
                )
        else:
            data_connector = execution_environment.get_data_connector(
                data_connector_name
            )
            if data_connector == "dummy_data_connector":
                raise ValueError(
                    "No in_memory_dataset found. To use the dummy_data_connector, please ensure that you"
                    "are passing a dataset to load_batch()"
                )
            batch_spec = data_connector.build_batch_spec(batch_definition=batch_definition)

            # We will use and manipulate reader_options along the way
            reader_options = batch_spec.get("reader_options", {})

            if isinstance(batch_spec, PathBatchSpec):
                path = batch_spec["path"]
                reader_method = batch_spec.get("reader_method")
                reader_fn = self._get_reader_fn(reader_method, path)
                df = reader_fn(path, **reader_options)

            elif isinstance(batch_spec, S3BatchSpec):
                url, s3_object = data_connector.get_s3_object(batch_spec=batch_spec)
                reader_method = batch_spec.get("reader_method")
                reader_fn = self._get_reader_fn(reader_method, url.key)
                df = reader_fn(
                    StringIO(
                        s3_object["Body"]
                        .read()
                        .decode(s3_object.get("ContentEncoding", "utf-8"))
                    ),
                    **reader_options
                )

                # try:
                #     import boto3
                #
                #     s3 = boto3.client("s3", **self._boto3_options)
                # except ImportError:
                #     raise BatchSpecError(
                #         "Unable to load boto3 client to read s3 asset.", batch_spec
                #     )
                # raw_url = batch_spec["s3"]
                # reader_method = batch_spec.get("reader_method")
                # url = S3Url(raw_url)
                # logger.debug(
                #     "Fetching s3 object. Bucket: %s Key: %s" % (url.bucket, url.key)
                # )
                # s3_object = s3.get_object(Bucket=url.bucket, Key=url.key)
                # reader_fn = self._get_reader_fn(reader_method, url.key)
                # df = reader_fn(
                #     StringIO(
                #         s3_object["Body"]
                #             .read()
                #             .decode(s3_object.get("ContentEncoding", "utf-8"))
                #     ),
                #     **reader_options
                # )

            elif "dataset" in batch_spec and isinstance(
                batch_spec["dataset"], (pd.DataFrame, pd.Series)
            ):
                df = batch_spec.get("dataset")
                # We don't want to store the actual dataframe in kwargs; copy the remaining batch_spec
                batch_spec = {
                    k: batch_spec[k] for k in batch_spec if k != "dataset"
                }
                batch_spec["PandasInMemoryDF"] = True
                batch_spec["ge_batch_id"] = str(uuid.uuid1())

            else:
                raise BatchSpecError(
                    "Invalid batch_spec: path, s3, or df is required for a PandasDatasource",
                    batch_spec,
                )

        if df.memory_usage().sum() < HASH_THRESHOLD:
            batch_markers["pandas_data_fingerprint"] = hash_pandas_dataframe(df)

        self._batch = Batch(
            execution_environment_name=batch_definition.get("execution_environment"),
            batch_spec=batch_spec,
            data=df,
            batch_definition=batch_definition,
            batch_markers=batch_markers,
            data_context=self._data_context,
        )
        self._batch_spec = batch_spec
        self._batch_definition = batch_definition
        self._batch_markers = batch_markers

    @property
    def dataframe(self):
        if not self.batch:
            if self._batch_definition:
                self.load_batch(self._batch_definition)
            else:
                raise ValueError(
                    "Batch has not been loaded and no batch parameters were found. Please run "
                    "load_batch() to load a batch."
                )

        return self.batch.data

    def _get_reader_fn(self, reader_method=None, path=None):
        """Static helper for parsing reader types. If reader_method is not provided, path will be used to guess the
        correct reader_method.

        Args:
            reader_method (str): the name of the reader method to use, if available.
            path (str): the to use to guess

        Returns:
            ReaderMethod to use for the filepath

        """
        if reader_method is None and path is None:
            raise BatchSpecError(
                "Unable to determine pandas reader function without reader_method or path.",
                {"reader_method": reader_method},
            )

        reader_options = None
        if reader_method is None:
            path_guess = self.guess_reader_method_from_path(path)
            reader_method = path_guess["reader_method"]
            reader_options = path_guess.get(
                "reader_options"
            )  # This may not be there; use None in that case

        try:
            reader_fn = getattr(pd, reader_method)
            if reader_options:
                reader_fn = partial(reader_fn, **reader_options)
            return reader_fn
        except AttributeError:
            raise BatchSpecError(
                "Unable to find reader_method %s in pandas." % reader_method,
                {"reader_method": reader_method},
            )

    @staticmethod
    def guess_reader_method_from_path(path):
        if path.endswith(".csv") or path.endswith(".tsv"):
            return {"reader_method": "read_csv"}
        elif path.endswith(".parquet"):
            return {"reader_method": "read_parquet"}
        elif path.endswith(".xlsx") or path.endswith(".xls"):
            return {"reader_method": "read_excel"}
        elif path.endswith(".json"):
            return {"reader_method": "read_json"}
        elif path.endswith(".pkl"):
            return {"reader_method": "read_pickle"}
        elif path.endswith(".feather"):
            return {"reader_method": "read_feather"}
        elif path.endswith(".csv.gz") or path.endswith(".csv.gz"):
            return {
                "reader_method": "read_csv",
                "reader_options": {"compression": "gzip"},
            }

        raise BatchSpecError(
            "Unable to determine reader method from path: %s" % path, {"path": path}
        )

    def process_batch_definition(
        self,
        batch_definition,
        batch_spec
    ):
        limit = batch_definition.get("limit")

        if limit is not None:
            if not batch_spec.get("reader_options"):
                batch_spec["reader_options"] = dict()
            batch_spec["reader_options"]["nrows"] = limit

        # TODO: Make sure dataset_options are accounted for in __init__ of ExecutionEngine
        # if dataset_options is not None:
        #     # Then update with any locally-specified reader options
        #     if not batch_parameters.get("dataset_options"):
        #         batch_parameters["dataset_options"] = dict()
        #     batch_parameters["dataset_options"].update(dataset_options)

        return batch_spec

    def get_row_count(self):
        return self.dataframe.shape[0]

    def get_column_count(self):
        return self.dataframe.shape[1]

    def get_table_columns(self) -> List[str]:
        return list(self.dataframe.columns)

    def get_column_sum(self, column):
        return self.dataframe[column].sum()

    def get_column_max(self, column, parse_strings_as_datetimes=False):
        temp_column = self.dataframe[column].dropna()
        if parse_strings_as_datetimes:
            temp_column = temp_column.map(parse)
        return temp_column.max()

    def get_column_min(self, column, parse_strings_as_datetimes=False):
        temp_column = self.dataframe[column].dropna()
        if parse_strings_as_datetimes:
            temp_column = temp_column.map(parse)
        return temp_column.min()

    def get_column_mean(self, column):
        return self.dataframe[column].mean()

    def get_column_nonnull_count(self, column):
        series = self.dataframe[column]
        null_indexes = series.isnull()
        nonnull_values = series[null_indexes == False]
        return len(nonnull_values)

    def get_column_value_counts(self, column, sort="value", collate=None):
        if sort not in ["value", "count", "none"]:
            raise ValueError("sort must be either 'value', 'count', or 'none'")
        if collate is not None:
            raise ValueError(
                "collate parameter is not supported in PandasExecutionEngine"
            )
        counts = self.dataframe[column].value_counts()
        if sort == "value":
            try:
                counts.sort_index(inplace=True)
            except TypeError:
                # Having values of multiple types in a object dtype column (e.g., strings and floats)
                # raises a TypeError when the sorting method performs comparisons.
                if self.dataframe[column].dtype == object:
                    counts.index = counts.index.astype(str)
                    counts.sort_index(inplace=True)
        elif sort == "counts":
            counts.sort_values(inplace=True)
        counts.name = "count"
        counts.index.name = "value"
        return counts

    def get_column_unique_count(self, column):
        return self.dataframe.get_column_value_counts(column).shape[0]

    def get_column_modes(self, column):
        return list(self.dataframe[column].mode().values)

    def get_column_median(self, column):
        return self.dataframe[column].median()

    def get_column_quantiles(self, column, quantiles, allow_relative_error=False):
        if allow_relative_error is not False:
            raise ValueError(
                "PandasExecutionEngine does not support relative error in column quantiles."
            )
        return (
            self.dataframe[column].quantile(quantiles, interpolation="nearest").tolist()
        )

    def get_column_stdev(self, column):
        return self.dataframe[column].std()

    def get_column_hist(self, column, bins):
        hist, bin_edges = np.histogram(self.dataframe[column], bins, density=False)
        return list(hist)

    def get_column_count_in_range(
        self, column, min_val=None, max_val=None, strict_min=False, strict_max=True
    ):
        # TODO this logic could probably go in the non-underscore version if we want to cache
        if min_val is None and max_val is None:
            raise ValueError("Must specify either min or max value")
        if min_val is not None and max_val is not None and min_val > max_val:
            raise ValueError("Min value must be <= to max value")

        result = self.dataframe[column]
        if min_val is not None:
            if strict_min:
                result = result[result > min_val]
            else:
                result = result[result >= min_val]
        if max_val is not None:
            if strict_max:
                result = result[result < max_val]
            else:
                result = result[result <= max_val]
        return len(result)

    ### Expectation methods ###

    @DocInherit
    @MetaPandasExecutionEngine.column_map_expectation
    def expect_column_values_to_be_unique(
        self,
        column,
        mostly=None,
        result_format=None,
        row_condition=None,
        condition_parser=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):

        return ~column.duplicated(keep=False)

    @DocInherit
    @MetaPandasExecutionEngine.column_map_expectation
    def expect_column_values_to_not_be_null(
        self,
        column,
        mostly=None,
        result_format=None,
        row_condition=None,
        condition_parser=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
        include_nulls=True,
    ):

        return ~column.isnull()

    @DocInherit
    @MetaPandasExecutionEngine.column_map_expectation
    def expect_column_values_to_be_null(
        self,
        column,
        mostly=None,
        result_format=None,
        row_condition=None,
        condition_parser=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):

        return column.isnull()

    @DocInherit
    def expect_column_values_to_be_of_type(
        self,
        column,
        type_,
        **kwargs
        # Since we've now received the default arguments *before* the expectation decorator, we need to
        # ensure we only pass what we actually received. Hence, we'll use kwargs
        # mostly=None,
        # result_format=None,
        # row_condition=None, condition_parser=None, include_config=None, catch_exceptions=None, meta=None
    ):
        """
        The pandas implementation of this expectation takes kwargs mostly, result_format, include_config,
        catch_exceptions, and meta as other expectations, however it declares **kwargs because it needs to
        be able to fork into either aggregate or map semantics depending on the column type (see below).

        In Pandas, columns *may* be typed, or they may be of the generic "object" type which can include rows with
        different storage types in the same column.

        To respect that implementation, the expect_column_values_to_be_of_type expectations will first attempt to
        use the column dtype information to determine whether the column is restricted to the provided type. If that
        is possible, then expect_column_values_to_be_of_type will return aggregate information including an
        observed_value, similarly to other backends.

        If it is not possible (because the column dtype is "object" but a more specific type was specified), then
        PandasExecutionEngine will use column map semantics: it will return map expectation results and
        check each value individually, which can be substantially slower.

        Unfortunately, the "object" type is also used to contain any string-type columns (including 'str' and
        numpy 'string_' (bytes)); consequently, it is not possible to test for string columns using aggregate semantics.
        """
        # Short-circuit if the dtype tells us; in that case use column-aggregate (vs map) semantics
        if (
            self[column].dtype != "object"
            or type_ is None
            or type_ in ["object", "object_", "O"]
        ):
            res = self._expect_column_values_to_be_of_type__aggregate(
                column, type_, **kwargs
            )
            # Note: this logic is similar to the logic in _append_expectation for deciding when to overwrite an
            # existing expectation, but it should be definitely kept in sync

            # We do not need this bookkeeping if we are in an active validation:
            if self._active_validation:
                return res

            # First, if there is an existing expectation of this type, delete it. Then change the one we created to be
            # of the proper expectation_type
            existing_expectations = self._expectation_suite.find_expectation_indexes(
                ExpectationConfiguration(
                    expectation_type="expect_column_values_to_be_of_type",
                    kwargs={"column": column},
                )
            )
            if len(existing_expectations) == 1:
                self._expectation_suite.expectations.pop(existing_expectations[0])

            # Now, rename the expectation we just added
            new_expectations = self._expectation_suite.find_expectation_indexes(
                ExpectationConfiguration(
                    expectation_type="_expect_column_values_to_be_of_type__aggregate",
                    kwargs={"column": column},
                )
            )
            assert len(new_expectations) == 1
            old_config = self._expectation_suite.expectations[new_expectations[0]]
            new_config = ExpectationConfiguration(
                expectation_type="expect_column_values_to_be_of_type",
                kwargs=old_config.kwargs,
                meta=old_config.meta,
                success_on_last_run=old_config.success_on_last_run,
            )
            self._expectation_suite.expectations[new_expectations[0]] = new_config
        else:
            res = self._expect_column_values_to_be_of_type__map(column, type_, **kwargs)
            # Note: this logic is similar to the logic in _append_expectation for deciding when to overwrite an
            # existing expectation, but it should be definitely kept in sync

            # We do not need this bookkeeping if we are in an active validation:
            if self._active_validation:
                return res

            # First, if there is an existing expectation of this type, delete it. Then change the one we created to be
            # of the proper expectation_type
            existing_expectations = self._expectation_suite.find_expectation_indexes(
                ExpectationConfiguration(
                    expectation_type="expect_column_values_to_be_of_type",
                    kwargs={"column": column},
                )
            )
            if len(existing_expectations) == 1:
                self._expectation_suite.expectations.pop(existing_expectations[0])

            # Now, rename the expectation we just added
            new_expectations = self._expectation_suite.find_expectation_indexes(
                ExpectationConfiguration(
                    expectation_type="_expect_column_values_to_be_of_type__map",
                    kwargs={"column": column},
                )
            )
            assert len(new_expectations) == 1
            old_config = self._expectation_suite.expectations[new_expectations[0]]
            new_config = ExpectationConfiguration(
                expectation_type="expect_column_values_to_be_of_type",
                kwargs=old_config.kwargs,
                meta=old_config.meta,
                success_on_last_run=old_config.success_on_last_run,
            )
            self._expectation_suite.expectations[new_expectations[0]] = new_config

        return res

    @DataAsset.expectation(["column", "type_", "mostly"])
    def _expect_column_values_to_be_of_type__aggregate(
        self,
        column,
        type_,
        mostly=None,
        result_format=None,
        row_condition=None,
        condition_parser=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        if mostly is not None:
            raise ValueError(
                "PandasExecutionEngine cannot support mostly for a column with a non-object dtype."
            )

        if type_ is None:
            success = True
        else:
            comp_types = []
            try:
                comp_types.append(np.dtype(type_).type)
            except TypeError:
                try:
                    pd_type = getattr(pd, type_)
                    if isinstance(pd_type, type):
                        comp_types.append(pd_type)
                except AttributeError:
                    pass

                try:
                    pd_type = getattr(pd.core.dtypes.dtypes, type_)
                    if isinstance(pd_type, type):
                        comp_types.append(pd_type)
                except AttributeError:
                    pass

            native_type = self._native_type_type_map(type_)
            if native_type is not None:
                comp_types.extend(native_type)
            success = self[column].dtype.type in comp_types

        return {
            "success": success,
            "result": {"observed_value": self[column].dtype.type.__name__},
        }

    @staticmethod
    def _native_type_type_map(type_):
        # We allow native python types in cases where the underlying type is "object":
        if type_.lower() == "none":
            return (type(None),)
        elif type_.lower() == "bool":
            return (bool,)
        elif type_.lower() in ["int", "long"]:
            return (int,)
        elif type_.lower() == "float":
            return (float,)
        elif type_.lower() == "bytes":
            return (bytes,)
        elif type_.lower() == "complex":
            return (complex,)
        elif type_.lower() in ["str", "string_types"]:
            return (str,)
        elif type_.lower() == "list":
            return (list,)
        elif type_.lower() == "dict":
            return (dict,)
        elif type_.lower() == "unicode":
            return None

    @MetaPandasExecutionEngine.column_map_expectation
    def _expect_column_values_to_be_of_type__map(
        self,
        column,
        type_,
        mostly=None,
        result_format=None,
        row_condition=None,
        condition_parser=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):

        comp_types = []
        try:
            comp_types.append(np.dtype(type_).type)
        except TypeError:
            try:
                pd_type = getattr(pd, type_)
                if isinstance(pd_type, type):
                    comp_types.append(pd_type)
            except AttributeError:
                pass

            try:
                pd_type = getattr(pd.core.dtypes.dtypes, type_)
                if isinstance(pd_type, type):
                    comp_types.append(pd_type)
            except AttributeError:
                pass

        native_type = self._native_type_type_map(type_)
        if native_type is not None:
            comp_types.extend(native_type)

        if len(comp_types) < 1:
            raise ValueError("Unrecognized numpy/python type: %s" % type_)

        return column.map(lambda x: isinstance(x, tuple(comp_types)))

    @DocInherit
    def expect_column_values_to_be_in_type_list(
        self,
        column,
        type_list,
        **kwargs
        # Since we've now received the default arguments *before* the expectation decorator, we need to
        # ensure we only pass what we actually received. Hence, we'll use kwargs
        # mostly=None,
        # result_format = None,
        # row_condition=None, condition_parser=None, include_config=None, catch_exceptions=None, meta=None
    ):
        """
        The pandas implementation of this expectation takes kwargs mostly, result_format, include_config,
        catch_exceptions, and meta as other expectations, however it declares **kwargs because it needs to
        be able to fork into either aggregate or map semantics depending on the column type (see below).

        In Pandas, columns *may* be typed, or they may be of the generic "object" type which can include rows with
        different storage types in the same column.

        To respect that implementation, the expect_column_values_to_be_of_type expectations will first attempt to
        use the column dtype information to determine whether the column is restricted to the provided type. If that
        is possible, then expect_column_values_to_be_of_type will return aggregate information including an
        observed_value, similarly to other backends.

        If it is not possible (because the column dtype is "object" but a more specific type was specified), then
        PandasExecutionEngine will use column map semantics: it will return map expectation results and
        check each value individually, which can be substantially slower.

        Unfortunately, the "object" type is also used to contain any string-type columns (including 'str' and
        numpy 'string_' (bytes)); consequently, it is not possible to test for string columns using aggregate semantics.
        """
        # Short-circuit if the dtype tells us; in that case use column-aggregate (vs map) semantics
        if self[column].dtype != "object" or type_list is None:
            res = self._expect_column_values_to_be_in_type_list__aggregate(
                column, type_list, **kwargs
            )
            # Note: this logic is similar to the logic in _append_expectation for deciding when to overwrite an
            # existing expectation, but it should be definitely kept in sync

            # We do not need this bookkeeping if we are in an active validation:
            if self._active_validation:
                return res

            # First, if there is an existing expectation of this type, delete it. Then change the one we created to be
            # of the proper expectation_type
            existing_expectations = self._expectation_suite.find_expectation_indexes(
                ExpectationConfiguration(
                    expectation_type="expect_column_values_to_be_in_type_list",
                    kwargs={"column": column},
                )
            )
            if len(existing_expectations) == 1:
                self._expectation_suite.expectations.pop(existing_expectations[0])

            new_expectations = self._expectation_suite.find_expectation_indexes(
                ExpectationConfiguration(
                    expectation_type="_expect_column_values_to_be_in_type_list__aggregate",
                    kwargs={"column": column},
                )
            )
            assert len(new_expectations) == 1
            old_config = self._expectation_suite.expectations[new_expectations[0]]
            new_config = ExpectationConfiguration(
                expectation_type="expect_column_values_to_be_in_type_list",
                kwargs=old_config.kwargs,
                meta=old_config.meta,
                success_on_last_run=old_config.success_on_last_run,
            )
            self._expectation_suite.expectations[new_expectations[0]] = new_config
        else:
            res = self._expect_column_values_to_be_in_type_list__map(
                column, type_list, **kwargs
            )
            # Note: this logic is similar to the logic in _append_expectation for deciding when to overwrite an
            # existing expectation, but it should be definitely kept in sync

            # We do not need this bookkeeping if we are in an active validation:
            if self._active_validation:
                return res

            # First, if there is an existing expectation of this type, delete it. Then change the one we created to be
            # of the proper expectation_type
            existing_expectations = self._expectation_suite.find_expectation_indexes(
                ExpectationConfiguration(
                    expectation_type="expect_column_values_to_be_in_type_list",
                    kwargs={"column": column},
                )
            )
            if len(existing_expectations) == 1:
                self._expectation_suite.expectations.pop(existing_expectations[0])

            # Now, rename the expectation we just added
            new_expectations = self._expectation_suite.find_expectation_indexes(
                ExpectationConfiguration(
                    expectation_type="_expect_column_values_to_be_in_type_list__map",
                    kwargs={"column": column},
                )
            )
            assert len(new_expectations) == 1
            old_config = self._expectation_suite.expectations[new_expectations[0]]
            new_config = ExpectationConfiguration(
                expectation_type="expect_column_values_to_be_in_type_list",
                kwargs=old_config.kwargs,
                meta=old_config.meta,
                success_on_last_run=old_config.success_on_last_run,
            )
            self._expectation_suite.expectations[new_expectations[0]] = new_config

        return res

    @MetaPandasExecutionEngine.expectation(["column", "type_list", "mostly"])
    def _expect_column_values_to_be_in_type_list__aggregate(
        self,
        column,
        type_list,
        mostly=None,
        result_format=None,
        row_condition=None,
        condition_parser=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        if mostly is not None:
            raise ValueError(
                "PandasExecutionEngine cannot support mostly for a column with a non-object dtype."
            )

        if type_list is None:
            success = True
        else:
            comp_types = []
            for type_ in type_list:
                try:
                    comp_types.append(np.dtype(type_).type)
                except TypeError:
                    try:
                        pd_type = getattr(pd, type_)
                        if isinstance(pd_type, type):
                            comp_types.append(pd_type)
                    except AttributeError:
                        pass

                    try:
                        pd_type = getattr(pd.core.dtypes.dtypes, type_)
                        if isinstance(pd_type, type):
                            comp_types.append(pd_type)
                    except AttributeError:
                        pass

                native_type = self._native_type_type_map(type_)
                if native_type is not None:
                    comp_types.extend(native_type)

            success = self[column].dtype.type in comp_types

        return {
            "success": success,
            "result": {"observed_value": self[column].dtype.type.__name__},
        }

    @MetaPandasExecutionEngine.column_map_expectation
    def _expect_column_values_to_be_in_type_list__map(
        self,
        column,
        type_list,
        mostly=None,
        result_format=None,
        row_condition=None,
        condition_parser=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):

        comp_types = []
        for type_ in type_list:
            try:
                comp_types.append(np.dtype(type_).type)
            except TypeError:
                try:
                    pd_type = getattr(pd, type_)
                    if isinstance(pd_type, type):
                        comp_types.append(pd_type)
                except AttributeError:
                    pass

                try:
                    pd_type = getattr(pd.core.dtypes.dtypes, type_)
                    if isinstance(pd_type, type):
                        comp_types.append(pd_type)
                except AttributeError:
                    pass

            native_type = self._native_type_type_map(type_)
            if native_type is not None:
                comp_types.extend(native_type)

        if len(comp_types) < 1:
            raise ValueError("No recognized numpy/python type in list: %s" % type_list)

        return column.map(lambda x: isinstance(x, tuple(comp_types)))

    @DocInherit
    @MetaPandasExecutionEngine.column_map_expectation
    def expect_column_values_to_be_in_set(
        self,
        column,
        value_set,
        mostly=None,
        parse_strings_as_datetimes=None,
        result_format=None,
        row_condition=None,
        condition_parser=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        if value_set is None:
            # Vacuously true
            return np.ones(len(column), dtype=np.bool_)

        if parse_strings_as_datetimes:
            parsed_value_set = self._parse_value_set(value_set)
        else:
            parsed_value_set = value_set

        return column.isin(parsed_value_set)

    @DocInherit
    @MetaPandasExecutionEngine.column_map_expectation
    def expect_column_values_to_not_be_in_set(
        self,
        column,
        value_set,
        mostly=None,
        parse_strings_as_datetimes=None,
        result_format=None,
        row_condition=None,
        condition_parser=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        if parse_strings_as_datetimes:
            parsed_value_set = self._parse_value_set(value_set)
        else:
            parsed_value_set = value_set

        return ~column.isin(parsed_value_set)

    @DocInherit
    @MetaPandasExecutionEngine.column_map_expectation
    def expect_column_values_to_be_between(
        self,
        column,
        min_value=None,
        max_value=None,
        strict_min=False,
        strict_max=False,  # tolerance=1e-9,
        parse_strings_as_datetimes=None,
        output_strftime_format=None,
        allow_cross_type_comparisons=None,
        mostly=None,
        row_condition=None,
        condition_parser=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        if min_value is None and max_value is None:
            raise ValueError("min_value and max_value cannot both be None")

        # if strict_min and min_value:
        #     min_value += tolerance
        #
        # if strict_max and max_value:
        #     max_value -= tolerance

        if parse_strings_as_datetimes:
            # tolerance = timedelta(days=tolerance)
            if min_value:
                min_value = parse(min_value)

            if max_value:
                max_value = parse(max_value)

            try:
                temp_column = column.map(parse)
            except TypeError:
                temp_column = column

        else:
            temp_column = column

        if min_value is not None and max_value is not None and min_value > max_value:
            raise ValueError("min_value cannot be greater than max_value")

        def is_between(val):
            # TODO Might be worth explicitly defining comparisons between types (for example, between strings and ints).
            # Ensure types can be compared since some types in Python 3 cannot be logically compared.
            # print type(val), type(min_value), type(max_value), val, min_value, max_value

            if type(val) is None:
                return False

            if min_value is not None and max_value is not None:
                if allow_cross_type_comparisons:
                    try:
                        if strict_min and strict_max:
                            return (min_value < val) and (val < max_value)
                        elif strict_min:
                            return (min_value < val) and (val <= max_value)
                        elif strict_max:
                            return (min_value <= val) and (val < max_value)
                        else:
                            return (min_value <= val) and (val <= max_value)
                    except TypeError:
                        return False

                else:
                    if (isinstance(val, str) != isinstance(min_value, str)) or (
                        isinstance(val, str) != isinstance(max_value, str)
                    ):
                        raise TypeError(
                            "Column values, min_value, and max_value must either be None or of the same type."
                        )

                    if strict_min and strict_max:
                        return (min_value < val) and (val < max_value)
                    elif strict_min:
                        return (min_value < val) and (val <= max_value)
                    elif strict_max:
                        return (min_value <= val) and (val < max_value)
                    else:
                        return (min_value <= val) and (val <= max_value)

            elif min_value is None and max_value is not None:
                if allow_cross_type_comparisons:
                    try:
                        if strict_max:
                            return val < max_value
                        else:
                            return val <= max_value
                    except TypeError:
                        return False

                else:
                    if isinstance(val, str) != isinstance(max_value, str):
                        raise TypeError(
                            "Column values, min_value, and max_value must either be None or of the same type."
                        )

                    if strict_max:
                        return val < max_value
                    else:
                        return val <= max_value

            elif min_value is not None and max_value is None:
                if allow_cross_type_comparisons:
                    try:
                        if strict_min:
                            return min_value < val
                        else:
                            return min_value <= val
                    except TypeError:
                        return False

                else:
                    if isinstance(val, str) != isinstance(min_value, str):
                        raise TypeError(
                            "Column values, min_value, and max_value must either be None or of the same type."
                        )

                    if strict_min:
                        return min_value < val
                    else:
                        return min_value <= val

            else:
                return False

        return temp_column.map(is_between)

    @DocInherit
    @MetaPandasExecutionEngine.column_map_expectation
    def expect_column_values_to_be_increasing(
        self,
        column,
        strictly=None,
        parse_strings_as_datetimes=None,
        output_strftime_format=None,
        mostly=None,
        row_condition=None,
        condition_parser=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        if parse_strings_as_datetimes:
            temp_column = column.map(parse)

            col_diff = temp_column.diff()

            # The first element is null, so it gets a bye and is always treated as True
            col_diff[0] = pd.Timedelta(1)

            if strictly:
                return col_diff > pd.Timedelta(0)
            else:
                return col_diff >= pd.Timedelta(0)

        else:
            col_diff = column.diff()
            # The first element is null, so it gets a bye and is always treated as True
            col_diff[col_diff.isnull()] = 1

            if strictly:
                return col_diff > 0
            else:
                return col_diff >= 0

    @DocInherit
    @MetaPandasExecutionEngine.column_map_expectation
    def expect_column_values_to_be_decreasing(
        self,
        column,
        strictly=None,
        parse_strings_as_datetimes=None,
        output_strftime_format=None,
        mostly=None,
        row_condition=None,
        condition_parser=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        if parse_strings_as_datetimes:
            temp_column = column.map(parse)

            col_diff = temp_column.diff()

            # The first element is null, so it gets a bye and is always treated as True
            col_diff[0] = pd.Timedelta(-1)

            if strictly:
                return col_diff < pd.Timedelta(0)
            else:
                return col_diff <= pd.Timedelta(0)

        else:
            col_diff = column.diff()
            # The first element is null, so it gets a bye and is always treated as True
            col_diff[col_diff.isnull()] = -1

            if strictly:
                return col_diff < 0
            else:
                return col_diff <= 0

    @DocInherit
    @MetaPandasExecutionEngine.column_map_expectation
    def expect_column_value_lengths_to_be_between(
        self,
        column,
        min_value=None,
        max_value=None,
        mostly=None,
        row_condition=None,
        condition_parser=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):

        if min_value is None and max_value is None:
            raise ValueError("min_value and max_value cannot both be None")

        # Assert that min_value and max_value are integers
        try:
            if min_value is not None and not float(min_value).is_integer():
                raise ValueError("min_value and max_value must be integers")

            if max_value is not None and not float(max_value).is_integer():
                raise ValueError("min_value and max_value must be integers")

        except ValueError:
            raise ValueError("min_value and max_value must be integers")

        column_lengths = column.astype(str).str.len()

        if min_value is not None and max_value is not None:
            return column_lengths.between(min_value, max_value)

        elif min_value is None and max_value is not None:
            return column_lengths <= max_value

        elif min_value is not None and max_value is None:
            return column_lengths >= min_value

        else:
            return False

    @DocInherit
    @MetaPandasExecutionEngine.column_map_expectation
    def expect_column_value_lengths_to_equal(
        self,
        column,
        value,
        mostly=None,
        result_format=None,
        row_condition=None,
        condition_parser=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        return column.str.len() == value

    @DocInherit
    @MetaPandasExecutionEngine.column_map_expectation
    def expect_column_values_to_match_regex(
        self,
        column,
        regex,
        mostly=None,
        result_format=None,
        row_condition=None,
        condition_parser=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        return column.astype(str).str.contains(regex)

    @DocInherit
    @MetaPandasExecutionEngine.column_map_expectation
    def expect_column_values_to_not_match_regex(
        self,
        column,
        regex,
        mostly=None,
        result_format=None,
        row_condition=None,
        condition_parser=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        return ~column.astype(str).str.contains(regex)

    @DocInherit
    @MetaPandasExecutionEngine.column_map_expectation
    def expect_column_values_to_match_regex_list(
        self,
        column,
        regex_list,
        match_on="any",
        mostly=None,
        result_format=None,
        row_condition=None,
        condition_parser=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):

        regex_matches = []
        for regex in regex_list:
            regex_matches.append(column.astype(str).str.contains(regex))
        regex_match_df = pd.concat(regex_matches, axis=1, ignore_index=True)

        if match_on == "any":
            return regex_match_df.any(axis="columns")
        elif match_on == "all":
            return regex_match_df.all(axis="columns")
        else:
            raise ValueError("match_on must be either 'any' or 'all'")

    @DocInherit
    @MetaPandasExecutionEngine.column_map_expectation
    def expect_column_values_to_not_match_regex_list(
        self,
        column,
        regex_list,
        mostly=None,
        result_format=None,
        row_condition=None,
        condition_parser=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        regex_matches = []
        for regex in regex_list:
            regex_matches.append(column.astype(str).str.contains(regex))
        regex_match_df = pd.concat(regex_matches, axis=1, ignore_index=True)

        return ~regex_match_df.any(axis="columns")

    @DocInherit
    @MetaPandasExecutionEngine.column_map_expectation
    def expect_column_values_to_match_strftime_format(
        self,
        column,
        strftime_format,
        mostly=None,
        result_format=None,
        row_condition=None,
        condition_parser=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        # Below is a simple validation that the provided format can both format and parse a datetime object.
        # %D is an example of a format that can format but not parse, e.g.
        try:
            datetime.datetime.strptime(
                datetime.datetime.strftime(datetime.datetime.now(), strftime_format),
                strftime_format,
            )
        except ValueError as e:
            raise ValueError("Unable to use provided strftime_format. " + str(e))

        def is_parseable_by_format(val):
            try:
                datetime.datetime.strptime(val, strftime_format)
                return True
            except TypeError:
                raise TypeError(
                    "Values passed to expect_column_values_to_match_strftime_format must be of type string.\nIf you want to validate a column of dates or timestamps, please call the expectation before converting from string format."
                )
            except ValueError:
                return False

        return column.map(is_parseable_by_format)

    @DocInherit
    @MetaPandasExecutionEngine.column_map_expectation
    def expect_column_values_to_be_dateutil_parseable(
        self,
        column,
        mostly=None,
        result_format=None,
        row_condition=None,
        condition_parser=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        def is_parseable(val):
            try:
                if type(val) != str:
                    raise TypeError(
                        "Values passed to expect_column_values_to_be_dateutil_parseable must be of type string.\nIf you want to validate a column of dates or timestamps, please call the expectation before converting from string format."
                    )

                parse(val)
                return True

            except (ValueError, OverflowError):
                return False

        return column.map(is_parseable)

    @DocInherit
    @MetaPandasExecutionEngine.column_map_expectation
    def expect_column_values_to_be_json_parseable(
        self,
        column,
        mostly=None,
        result_format=None,
        row_condition=None,
        condition_parser=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        def is_json(val):
            try:
                json.loads(val)
                return True
            except:
                return False

        return column.map(is_json)

    @DocInherit
    @MetaPandasExecutionEngine.column_map_expectation
    def expect_column_values_to_match_json_schema(
        self,
        column,
        json_schema,
        mostly=None,
        result_format=None,
        row_condition=None,
        condition_parser=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        def matches_json_schema(val):
            try:
                val_json = json.loads(val)
                jsonschema.validate(val_json, json_schema)
                # jsonschema.validate raises an error if validation fails.
                # So if we make it this far, we know that the validation succeeded.
                return True
            except jsonschema.ValidationError:
                return False
            except jsonschema.SchemaError:
                raise
            except:
                raise

        return column.map(matches_json_schema)

    @DocInherit
    @MetaPandasExecutionEngine.column_aggregate_expectation
    def expect_column_parameterized_distribution_ks_test_p_value_to_be_greater_than(
        self,
        column,
        distribution,
        p_value=0.05,
        params=None,
        result_format=None,
        row_condition=None,
        condition_parser=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        column = self[column]

        if p_value <= 0 or p_value >= 1:
            raise ValueError("p_value must be between 0 and 1 exclusive")

        # Validate params
        try:
            validate_distribution_parameters(distribution=distribution, params=params)
        except ValueError as e:
            raise e

        # Format arguments for scipy.kstest
        if isinstance(params, dict):
            positional_parameters = _scipy_distribution_positional_args_from_dict(
                distribution, params
            )
        else:
            positional_parameters = params

        # K-S Test
        ks_result = stats.kstest(column, distribution, args=positional_parameters)

        return {
            "success": ks_result[1] >= p_value,
            "result": {
                "observed_value": ks_result[1],
                "details": {
                    "expected_params": positional_parameters,
                    "observed_ks_result": ks_result,
                },
            },
        }

    @DocInherit
    @MetaPandasExecutionEngine.column_aggregate_expectation
    def expect_column_bootstrapped_ks_test_p_value_to_be_greater_than(
        self,
        column,
        partition_object=None,
        p=0.05,
        bootstrap_samples=None,
        bootstrap_sample_size=None,
        result_format=None,
        row_condition=None,
        condition_parser=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        column = self[column]

        if not is_valid_continuous_partition_object(partition_object):
            raise ValueError("Invalid continuous partition object.")

        # TODO: consider changing this into a check that tail_weights does not exist exclusively, by moving this check into is_valid_continuous_partition_object
        if (partition_object["bins"][0] == -np.inf) or (
            partition_object["bins"][-1] == np.inf
        ):
            raise ValueError("Partition endpoints must be finite.")

        if (
            "tail_weights" in partition_object
            and np.sum(partition_object["tail_weights"]) > 0
        ):
            raise ValueError(
                "Partition cannot have tail weights -- endpoints must be finite."
            )

        test_cdf = np.append(np.array([0]), np.cumsum(partition_object["weights"]))

        def estimated_cdf(x):
            return np.interp(x, partition_object["bins"], test_cdf)

        if bootstrap_samples is None:
            bootstrap_samples = 1000

        if bootstrap_sample_size is None:
            # Sampling too many elements (or not bootstrapping) will make the test too sensitive to the fact that we've
            # compressed via a partition.

            # Sampling too few elements will make the test insensitive to significant differences, especially
            # for nonoverlapping ranges.
            bootstrap_sample_size = len(partition_object["weights"]) * 2

        results = [
            stats.kstest(
                np.random.choice(column, size=bootstrap_sample_size), estimated_cdf
            )[1]
            for _ in range(bootstrap_samples)
        ]

        test_result = (1 + sum(x >= p for x in results)) / (bootstrap_samples + 1)

        hist, bin_edges = np.histogram(column, partition_object["bins"])
        below_partition = len(np.where(column < partition_object["bins"][0])[0])
        above_partition = len(np.where(column > partition_object["bins"][-1])[0])

        # Expand observed partition to report, if necessary
        if below_partition > 0 and above_partition > 0:
            observed_bins = (
                [np.min(column)] + partition_object["bins"] + [np.max(column)]
            )
            observed_weights = np.concatenate(
                ([below_partition], hist, [above_partition])
            ) / len(column)
        elif below_partition > 0:
            observed_bins = [np.min(column)] + partition_object["bins"]
            observed_weights = np.concatenate(([below_partition], hist)) / len(column)
        elif above_partition > 0:
            observed_bins = partition_object["bins"] + [np.max(column)]
            observed_weights = np.concatenate((hist, [above_partition])) / len(column)
        else:
            observed_bins = partition_object["bins"]
            observed_weights = hist / len(column)

        observed_cdf_values = np.cumsum(observed_weights)

        return_obj = {
            "success": test_result > p,
            "result": {
                "observed_value": test_result,
                "details": {
                    "bootstrap_samples": bootstrap_samples,
                    "bootstrap_sample_size": bootstrap_sample_size,
                    "observed_partition": {
                        "bins": observed_bins,
                        "weights": observed_weights.tolist(),
                    },
                    "expected_partition": {
                        "bins": partition_object["bins"],
                        "weights": partition_object["weights"],
                    },
                    "observed_cdf": {
                        "x": observed_bins,
                        "cdf_values": [0] + observed_cdf_values.tolist(),
                    },
                    "expected_cdf": {
                        "x": partition_object["bins"],
                        "cdf_values": test_cdf.tolist(),
                    },
                },
            },
        }

        return return_obj

    @DocInherit
    @MetaPandasExecutionEngine.column_pair_map_expectation
    def expect_column_pair_values_to_be_equal(
        self,
        column_A,
        column_B,
        ignore_row_if="both_values_are_missing",
        result_format=None,
        row_condition=None,
        condition_parser=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        return column_A == column_B

    @DocInherit
    @MetaPandasExecutionEngine.column_pair_map_expectation
    def expect_column_pair_values_A_to_be_greater_than_B(
        self,
        column_A,
        column_B,
        or_equal=None,
        parse_strings_as_datetimes=None,
        allow_cross_type_comparisons=None,
        ignore_row_if="both_values_are_missing",
        result_format=None,
        row_condition=None,
        condition_parser=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        # FIXME
        if allow_cross_type_comparisons == True:
            raise NotImplementedError

        if parse_strings_as_datetimes:
            temp_column_A = column_A.map(parse)
            temp_column_B = column_B.map(parse)

        else:
            temp_column_A = column_A
            temp_column_B = column_B

        if or_equal == True:
            return temp_column_A >= temp_column_B
        else:
            return temp_column_A > temp_column_B

    @DocInherit
    @MetaPandasExecutionEngine.column_pair_map_expectation
    def expect_column_pair_values_to_be_in_set(
        self,
        column_A,
        column_B,
        value_pairs_set,
        ignore_row_if="both_values_are_missing",
        result_format=None,
        row_condition=None,
        condition_parser=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        if value_pairs_set is None:
            # vacuously true
            return np.ones(len(column_A), dtype=np.bool_)

        temp_df = pd.DataFrame({"A": column_A, "B": column_B})
        value_pairs_set = {(x, y) for x, y in value_pairs_set}

        results = []
        for i, t in temp_df.iterrows():
            if pd.isnull(t["A"]):
                a = None
            else:
                a = t["A"]

            if pd.isnull(t["B"]):
                b = None
            else:
                b = t["B"]

            results.append((a, b) in value_pairs_set)

        return pd.Series(results, temp_df.index)

    @DocInherit
    @MetaPandasExecutionEngine.multicolumn_map_expectation
    def expect_multicolumn_values_to_be_unique(
        self,
        column_list,
        ignore_row_if="all_values_are_missing",
        result_format=None,
        row_condition=None,
        condition_parser=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        threshold = len(column_list.columns)
        # Do not dropna here, since we have separately dealt with na in decorator
        return column_list.nunique(dropna=False, axis=1) >= threshold
