"""
Forecasting Model Base Class
----------------------------

A forecasting model captures the future values of a time series as a function of the past as follows:

.. math:: y_{t+1} = f(y_t, y_{t-1}, ..., y_1),

where :math:`y_t` represents the time series' value(s) at time :math:`t`.
"""

from typing import Optional, Union, Any, Callable, List
from types import SimpleNamespace
from itertools import product
from abc import ABC, abstractmethod
import numpy as np
import pandas as pd

from ..timeseries import TimeSeries
from ..logging import get_logger, raise_log, raise_if_not
from ..utils import (
    _build_tqdm_iterator,
    _with_sanity_checks,
    get_timestamp_at_point,
    _historical_forecasts_general_checks
)
from .. import metrics

logger = get_logger(__name__)


class ForecastingModel(ABC):
    """ The base class for all forecasting models.

    All implementations of forecasting have to implement the `fit()` and `predict()` methods defined below.

    Attributes
    ----------
    training_series
        reference to the `TimeSeries` used for training the model.
    target_series
        reference to the `TimeSeries` used as target to train the model.
    """
    @abstractmethod
    def __init__(self):
        # Stores training date information:
        self.training_series: TimeSeries = None
        self.target_series: TimeSeries = None

        # state
        self._fit_called = False

    @abstractmethod
    def fit(self, training_series: TimeSeries, target_series: Optional[TimeSeries] = None) -> None:
        """ Fits/trains the model on the provided series

        Implements behavior that should happen when calling the `fit` method of every forcasting model regardless of
        wether they are univariate or multivariate.
        """
        self.training_series = training_series

        if target_series is None:
            target_series = training_series
        self.target_series = target_series

        raise_if_not(all(training_series.time_index() == target_series.time_index()),
                     "training and target series must have same time indices.",
                     logger)

        for series in (self.training_series, self.target_series):
            if series is not None:
                raise_if_not(len(series) >= self.min_train_series_length,
                             "Train series only contains {} elements but {} model requires at least {} entries"
                             .format(len(series), str(self), self.min_train_series_length),
                             logger)
        self._fit_called = True

    @abstractmethod
    def predict(self, n: int) -> TimeSeries:
        """ Predicts values for a certain number of time steps after the end of the training series

        Parameters
        ----------
        n
            The number of time steps after the end of the training time series for which to produce predictions

        Returns
        -------
        TimeSeries
            A time series containing the `n` next points, starting after the end of the training time series
        """

        if (not self._fit_called):
            raise_log(Exception('fit() must be called before predict()'), logger)

    @property
    def min_train_series_length(self) -> int:
        """
        Class property defining the minimum required length for the training series.
        This function/property should be overridden if a value higher than 3 is required.
        """
        return 3

    def _generate_new_dates(self, n: int) -> pd.DatetimeIndex:
        """
        Generates `n` new dates after the end of the training set
        """
        new_dates = [
            (self.training_series.time_index()[-1] + (i * self.training_series.freq())) for i in range(1, n + 1)
        ]
        return pd.DatetimeIndex(new_dates, freq=self.training_series.freq_str())

    def _build_forecast_series(self,
                               points_preds: np.ndarray) -> TimeSeries:
        """
        Builds a forecast time series starting after the end of the training time series, with the
        correct time index.
        """

        time_index = self._generate_new_dates(len(points_preds))

        return TimeSeries.from_times_and_values(time_index, points_preds, freq=self.training_series.freq())

    def _historical_forecasts_sanity_checks(self, *args: Any, **kwargs: Any) -> None:
        """Sanity checks for the historical_forecasts function

        Parameters
        ----------
        args
            The args parameter(s) provided to the historical_forecasts function.
        kwargs
            The kwargs paramter(s) provided to the historical_forecasts function.

        Raises
        ------
        ValueError
            when a check on the parameter does not pass.
        """
        # parse args and kwargs
        training_series = args[0]
        n = SimpleNamespace(**kwargs)

        # check target and training series
        target_series = n.target_series
        if target_series is None:
            target_series = training_series

        raise_if_not(all(training_series.time_index() == target_series.time_index()), "the target and training series"
                     " must have the same time indices.")

        _historical_forecasts_general_checks(training_series, kwargs)

    @_with_sanity_checks("_historical_forecasts_sanity_checks")
    def historical_forecasts(self,
                             training_series: TimeSeries,
                             target_series: Optional[TimeSeries] = None,
                             start: Union[pd.Timestamp, float, int] = 0.5,
                             forecast_horizon: int = 1,
                             stride: int = 1,
                             retrain: bool = True,
                             overlap_end: bool = False,
                             last_points_only: bool = True,
                             verbose: bool = False,
                             use_full_output_length: Optional[bool] = None) -> Union[TimeSeries, List[TimeSeries]]:

        """ Computes the historical forecasts the model would have produced with an expanding training window
        and (by default) returns a time series created from the last point of each of these individual forecasts

        To this end, it repeatedly builds a training set from the beginning of `training_series`. It trains the
        current model on the training set, emits a forecast of length equal to forecast_horizon, and then moves
        the end of the training set forward by `stride` time steps.

        By default, this method will return a single time series made up of the last point of each
        historical forecast. This time series will thus have a frequency of training_series.freq() * stride
        If `last_points_only` is set to False, it will instead return a list of the historical forecasts.

        By default, this method always re-trains the models on the entire available history,
        corresponding to an expanding window strategy.
        If `retrain` is set to False (useful for models with many parameter such as `TorchForecastingModel` instances
        like `RNNModel` and `TCNModel`), the model will only be trained on the initial training window
        (up to `start` time stamp), and only if it has not been trained before. Then, at every iteration, the
        newly expanded input sequence will be fed to the model to produce the new output.

        Parameters
        ----------
        training_series
            The training time series to use to compute the historical forecasts
        target_series
            The target time series to use to compute the historical forecasts. This parameter is only relevant for
            `MultivariateForecastingModel` instances. It allows for training on one `training_series`
            and predicting another `target_series`. In many multivariate forecasting problems, the
            `target_series` would constitute a subset of the components of the `training_series`.
            However, any combination of univariate and multivariate series is allowed here, as long
            as the indices all match up.
        start
            The first point at which a prediction is computed for a future time.
            This parameter supports 3 different data types: `float`, `int` and `pandas.Timestamp`.
            In the case of `float`, the parameter will be treated as the proportion of the time series
            that should lie before the first prediction point.
            In the case of `int`, the parameter will be treated as an integer index to the time index of
            `training_series` that will be used as first prediction time.
            In case of `pandas.Timestamp`, this time stamp will be used to determine the first prediction time
            directly.
        forecast_horizon
            The forecast horizon for the point predictions
        stride
            The number of time steps between two consecutive predictions.
        retrain
            Whether to retrain the model for every prediction or not. Currently only `TorchForecastingModel`
            instances such as `RNNModel` and `TCNModel` support setting `retrain` to `False`.
        use_full_output_length
            Optionally, if the model is an instance of `TorchForecastingModel`, this argument will be passed along
            as argument to the `predict` method of the model. Otherwise, if this value is set and the model is not an
            instance of `TorchForecastingModel`, this will cause an error.
        overlap_end
            Whether the returned forecasts can go beyond the series' end or not
        last_points_only
            Whether to retain only the last point of each historical forecast.
            If set to True, the method returns a single `TimeSeries` of the point forecasts.
            Otherwise returns a list of historical `TimeSeries` forecasts.
        verbose
            Whether to print progress

        Returns
        -------
        TimeSeries or List[TimeSeries]
            By default, a single TimeSeries instance created from the last point of each individual forecast.
            If `last_points_only` is set to False, a list of the historical forecasts.
        """
        # handle case where target_series not specified
        if target_series is None:
            target_series = training_series

        # construct predict kwargs dictionary
        predict_kwargs = {}
        if use_full_output_length is not None:
            predict_kwargs['use_full_output_length'] = use_full_output_length

        # construct fit function (used to ignore target series for univariate models)
        if isinstance(self, UnivariateForecastingModel):
            fit_function = lambda train, target, **kwargs: self.fit(train, **kwargs)  # noqa: E731
        else:
            fit_function = self.fit

        # prepare the start parameter -> pd.Timestamp
        start = get_timestamp_at_point(start, training_series)

        # build the prediction times in advance (to be able to use tqdm)
        if not overlap_end:
            last_valid_pred_time = training_series.time_index()[-1 - forecast_horizon]
        else:
            last_valid_pred_time = training_series.time_index()[-2]

        pred_times = [start]
        while pred_times[-1] < last_valid_pred_time:
            # compute the next prediction time and add it to pred times
            pred_times.append(pred_times[-1] + training_series.freq() * stride)

        # the last prediction time computed might have overshot last_valid_pred_time
        if pred_times[-1] > last_valid_pred_time:
            pred_times.pop(-1)

        iterator = _build_tqdm_iterator(pred_times, verbose)

        # iterate and forecast

        # Either store the whole forecasts or only the last points of each forecast, depending on last_points_only
        forecasts = []

        last_points_times = []
        last_points_values = []

        if not retrain and not self._fit_called:
            fit_function(training_series.drop_after(start), target_series.drop_after(start), verbose=verbose)

        for pred_time in iterator:
            train = training_series.drop_after(pred_time)   # build the training series
            target = target_series.drop_after(pred_time)    # build the target series

            if (retrain):
                fit_function(train, target)
                forecast = self.predict(forecast_horizon, **predict_kwargs)
            else:
                forecast = self.predict(forecast_horizon, input_series=train, **predict_kwargs)

            if last_points_only:
                last_points_values.append(forecast.values()[-1])
                last_points_times.append(forecast.end_time())
            else:
                forecasts.append(forecast)

        if last_points_only:
            return TimeSeries.from_times_and_values(pd.DatetimeIndex(last_points_times),
                                                    np.array(last_points_values),
                                                    freq=training_series.freq() * stride)
        return forecasts

    def backtest(self,
                 training_series: TimeSeries,
                 target_series: Optional[TimeSeries] = None,
                 start: Union[pd.Timestamp, float, int] = 0.5,
                 forecast_horizon: int = 1,
                 stride: int = 1,
                 retrain: bool = True,
                 overlap_end: bool = False,
                 last_points_only: bool = False,
                 metric: Callable[[TimeSeries, TimeSeries], float] = metrics.mape,
                 reduction: Union[Callable[[np.ndarray], float], None] = np.mean,
                 use_full_output_length: Optional[bool] = None,
                 verbose: bool = False) -> Union[float, List[float]]:

        """ Computes an error score between the historical forecasts the model would have produced
        with an expanding training window over `training_series` and the actual series.

        To this end, it repeatedly builds a training set from the beginning of `series`. It trains the current model on
        the training set, emits a forecast of length equal to forecast_horizon, and then moves the end of the
        training set forward by `stride` time steps.

        By default, this method will use each historical forecast (whole) to compute error scores.
        If `last_points_only` is set to True, it will use only the last point of each historical forecast.

        By default, this method always re-trains the models on the entire available history,
        corresponding to an expanding window strategy.
        If `retrain` is set to False (useful for models with many parameter such as `TorchForecastingModel` instances
        like `RNNModel` and `TCNModel`), the model will only be trained on the initial training window
        (up to `start` time stamp), and only if it has not been trained before. Then, at every iteration, the
        newly expanded input sequence will be fed to the model to produce the new output.

        Parameters
        ----------
        training_series
            The training time series to use to compute the historical forecasts
        target_series
            The target time series to use to compute the historical forecasts. This parameter is only relevant for
            `MultivariateForecastingModel` instances. It allows for training on one `training_series`
            and predicting another `target_series`. In many multivariate forecasting problems, the
            `target_series` would constitute a subset of the components of the `training_series`.
            However, any combination of univariate and multivariate series is allowed here, as long
            as the indices all match up.
        start
            The first prediction time, at which a prediction is computed for a future time.
            This parameter supports 3 different data types: `float`, `int` and `pandas.Timestamp`.
            In the case of `float`, the parameter will be treated as the proportion of the time series
            that should lie before the first prediction point.
            In the case of `int`, the parameter will be treated as an integer index to the time index of
            `training_series` that will be used as first prediction time.
            In case of `pandas.Timestamp`, this time stamp will be used to determine the first prediction time
            directly.
        forecast_horizon
            The forecast horizon for the point prediction
        stride
            The number of time steps (the unit being the frequency of `series`) between two consecutive predictions.
        retrain
            Whether to retrain the model for every prediction or not. Currently only `TorchForecastingModel`
            instances such as `RNNModel` and `TCNModel` support setting `retrain` to `False`.
        overlap_end
            Whether the returned forecasts can go beyond the series' end or not
        last_points_only
            Whether to use the whole historical forecasts or only the last point of each forecast to compute the error
        metric
            A function that takes two TimeSeries instances as inputs and returns a float error value.
        reduction
            A function used to combine the individual error scores obtained when `last_points_only` is set to False.
            If explicitely set to `None`, the method will return a list of the individual error scores instead.
            Set to np.mean by default.
        use_full_output_length
            Optionally, if the model is an instance of `TorchForecastingModel`, this argument will be passed along
            as argument to the `predict` method of the model. Otherwise, if this value is set and the model is not an
            instance of `TorchForecastingModel`, this will cause an error.
        verbose
            Whether to print progress

        Returns
        -------
        float or List[float]
            The error score, or the list of individual error scores if `reduction` is `None`
        """
        forecasts = self.historical_forecasts(training_series,
                                              target_series,
                                              start,
                                              forecast_horizon,
                                              stride,
                                              retrain,
                                              overlap_end,
                                              last_points_only,
                                              verbose,
                                              use_full_output_length)
        if target_series is None:
            target_series = training_series

        if last_points_only:
            return metric(target_series, forecasts)

        errors = []
        for forecast in forecasts:
            errors.append(metric(target_series, forecast))

        if reduction is None:
            return errors

        return reduction(errors)

    @classmethod
    def gridsearch(model_class,
                   parameters: dict,
                   training_series: TimeSeries,
                   target_series: Optional[TimeSeries] = None,
                   forecast_horizon: Optional[int] = None,
                   start: Union[pd.Timestamp, float, int] = 0.5,
                   last_points_only: bool = False,
                   use_full_output_length: Optional[bool] = None,
                   val_target_series: Optional[TimeSeries] = None,
                   use_fitted_values: bool = False,
                   metric: Callable[[TimeSeries, TimeSeries], float] = metrics.mape,
                   reduction: Callable[[np.ndarray], float] = np.mean,
                   verbose=False) -> TimeSeries:
        """ A function for finding the best hyperparameters.

        This function has 3 modes of operation: Expanding window mode, split mode and fitted value mode.
        The three modes of operation evaluate every possible combination of hyperparameter values
        provided in the `parameters` dictionary by instantiating the `model_class` subclass
        of ForecastingModel with each combination, and returning the best-performing model with regards
        to the `metric` function. The `metric` function is expected to return an error value,
        thus the model resulting in the smallest `metric` output will be chosen.
        The relationship of the training data and test data depends on the mode of operation.

        Expanding window mode (activated when `forecast_horizon` is passed):
        For every hyperparameter combination, the model is repeatedly trained and evaluated on different
        splits of `training_series` and `target_series`. This process is accomplished by using
        `ForecastingModel.backtest` as a subroutine to produce historic forecasts starting from `start`
        that are compared against the ground truth values of `training_series` or `target_series`, if
        specifed.
        Note that the model is retrained for every single prediction, thus this mode is slower.

        Split window mode (activated when `val_series` is passed):
        This mode will be used when the `val_series` argument is passed.
        For every hyperparameter combination, the model is trained on `training_series` + `target_series` and
        evaluated on `val_series`.

        Fitted value mode (activated when `use_fitted_values` is set to `True`):
        For every hyperparameter combination, the model is trained on `training_series` + `target_series`
        and evaluated on the resulting fitted values.
        Not all models have fitted values, and this method raises an error if `model.fitted_values` does not exist.
        The fitted values are the result of the fit of the model on the training series. Comparing with the
        fitted values can be a quick way to assess the model, but one cannot see if the model overfits or underfits.


        Parameters
        ----------
        model_class
            The ForecastingModel subclass to be tuned for 'series'.
        parameters
            A dictionary containing as keys hyperparameter names, and as values lists of values for the
            respective hyperparameter.
        training_series
            The TimeSeries instance used as input for training.
        target_series
            The TimeSeries instance used as target for training (and also validation in expanding window mode).
        forecast_horizon
            The integer value of the forecasting horizon used in expanding window mode.
        start
            The `int`, `float` or `pandas.Timestamp` that represents the starting point in the time index
            of `training_series` from which predictions will be made to evaluate the model.
            For a detailed description of how the different data types are interpreted, please see the documentation
            for `ForecastingModel.backtest`.
        last_points_only
            Whether to use the whole forecasts or only the last point of each forecast to compute the error
        use_full_output_length
            This should only be set if `model_class` is equal to `TorchForecastingModel`.
            This argument will be passed along to the predict method of `TorchForecastingModel`.
        val_target_series
            The TimeSeries instance used for validation in split mode.
        use_fitted_values
            If `True`, uses the comparison with the fitted values.
            Raises an error if `fitted_values` is not an attribute of `model_class`.
        metric
            A function that takes two TimeSeries instances as inputs and returns a float error value.
        verbose
            Whether to print progress.

        Returns
        -------
        ForecastingModel
            An untrained 'model_class' instance with the best-performing hyperparameters from the given selection.
        """
        raise_if_not((forecast_horizon is not None) + (val_target_series is not None) + use_fitted_values == 1,
                     "Please pass exactly one of the arguments 'forecast_horizon', "
                     "'val_target_series' or 'use_fitted_values'.", logger)

        if target_series is None:
            target_series = training_series
        # check target and training series
        raise_if_not(all(training_series.time_index() == target_series.time_index()),
                     "the target and training series must have the same time indices.",
                     logger)

        # construct predict kwargs dictionary
        predict_kwargs = {}
        if use_full_output_length is not None:
            predict_kwargs['use_full_output_length'] = use_full_output_length

        if use_fitted_values:
            raise_if_not(hasattr(model_class(), "fitted_values"),
                         "The model must have a fitted_values attribute to compare with the train TimeSeries",
                         logger)

        elif val_target_series is not None:
            raise_if_not(training_series.width == val_target_series.width,
                         "Training and validation series require the same number of components.",
                         logger)

        min_error = float('inf')
        best_param_combination = {}

        # compute all hyperparameter combinations from selection
        params_cross_product = list(product(*parameters.values()))

        # iterate through all combinations of the provided parameters and choose the best one
        iterator = _build_tqdm_iterator(params_cross_product, verbose)
        for param_combination in iterator:
            param_combination_dict = dict(list(zip(parameters.keys(), param_combination)))
            model = model_class(**param_combination_dict)
            if use_fitted_values:  # fitted value mode
                model.fit(training_series)
                fitted_values = TimeSeries.from_times_and_values(training_series.time_index(), model.fitted_values)
                error = metric(fitted_values, target_series)
            elif val_target_series is None:  # expanding window mode
                error = model.backtest(training_series,
                                       target_series,
                                       start,
                                       forecast_horizon,
                                       metric=metric,
                                       reduction=reduction,
                                       last_points_only=last_points_only,
                                       use_full_output_length=use_full_output_length)
            else:  # split mode
                if isinstance(model, MultivariateForecastingModel):
                    model.fit(training_series, target_series)
                else:
                    model.fit(training_series)
                error = metric(model.predict(len(val_target_series), **predict_kwargs), val_target_series)
            if error < min_error:
                min_error = error
                best_param_combination = param_combination_dict
        logger.info('Chosen parameters: ' + str(best_param_combination))

        return model_class(**best_param_combination)

    def residuals(self,
                  series: TimeSeries,
                  forecast_horizon: int = 1,
                  verbose: bool = False) -> TimeSeries:
        """ A function for computing the residuals produced by the current model on a univariate time series.

        This function computes the difference between the actual observations from `series`
        and the fitted values vector p obtained by training the model on `series`.
        For every index i in `series`, p[i] is computed by training the model on
        series[:(i - `forecast_horizon`)] and forecasting `forecast_horizon` into the future.
        (p[i] will be set to the last value of the predicted vector.)
        The vector of residuals will be shorter than `series` due to the minimum
        training series length required by the model and the gap introduced by `forecast_horizon`.
        Note that the common usage of the term residuals implies a value for `forecast_horizon` of 1.

        Parameters
        ----------
        series
            The univariate TimeSeries instance which the residuals will be computed for.
        forecast_horizon
            The forecasting horizon used to predict each fitted value.
        verbose
            Whether to print progress.
        Returns
        -------
        TimeSeries
            The vector of residuals.
        """

        series._assert_univariate()

        # get first index not contained in the first training set
        first_index = series.time_index()[self.min_train_series_length]

        # compute fitted values
        p = self.historical_forecasts(series,
                                      None,
                                      first_index,
                                      forecast_horizon,
                                      1,
                                      True,
                                      last_points_only=True,
                                      verbose=verbose)

        # compute residuals
        series_trimmed = series.slice_intersect(p)
        residuals = series_trimmed - p

        return residuals


class UnivariateForecastingModel(ForecastingModel):
    """The base class for univariate forecasting models."""
    @abstractmethod
    def fit(self, series: TimeSeries) -> None:
        """ Fits/trains the univariate model on selected univariate series.

        Implements behavior specific to calling the `fit` method on `UnivariateForecastingModel`.

        Parameters
        ----------
        series
            A **univariate** timeseries on which to fit the model.
        """
        series._assert_univariate()
        super().fit(series)


class MultivariateForecastingModel(ForecastingModel):
    """ The base class for multivariate forecasting models.
    """
    @abstractmethod
    def fit(self, training_series: TimeSeries, target_series: Optional[TimeSeries] = None) -> None:
        """ Fits/trains the multivariate model on the provided series with selected target components.

        Parameters
        ----------
        training_series
            The training time series on which to fit the model (can be multivariate or univariate).
        target_series
            The target values used as dependent variables when training the model
        """
        super().fit(training_series, target_series)
