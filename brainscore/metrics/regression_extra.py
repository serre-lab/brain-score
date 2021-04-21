import scipy.stats
import numpy as np
import os

from PIL import Image
import matplotlib.gridspec as gridspec
import matplotlib.pyplot as plt

from sklearn.cross_decomposition import PLSRegression
from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LinearRegression, Ridge
from sklearn.preprocessing import StandardScaler

from brainio_base.assemblies import walk_coords
from brainscore.metrics.mask_regression import MaskRegression
from brainscore.metrics.transformations import CrossValidation, ToleranceCrossValidation
from brainscore.metrics.regression import pls_regression
from .xarray_utils import XarrayRegression, XarrayCorrelation

from .xarray_utils import Defaults
from brainio_base.assemblies import NeuroidAssembly, array_is_element, walk_coords


class ToleranceCrossRegressedCorrelation:
    def __init__(self, regression, correlation, crossvalidation_kwargs=None):
        regression = regression or pls_regression()
        crossvalidation_defaults = dict(train_size=.9, test_size=None)
        crossvalidation_kwargs = {**crossvalidation_defaults, **(crossvalidation_kwargs or {})}

        self.cross_validation = ToleranceCrossValidation(**crossvalidation_kwargs)
        self.regression = regression
        self.correlation = correlation

    def __call__(self, source, target):
        return self.cross_validation(source, target, apply=self.apply, aggregate=self.aggregate)

    def apply(self, source_train, target_train, source_test, target_test):
        self.regression.fit(source_train, target_train)
        prediction = self.regression.predict(source_test)
        score = self.correlation(prediction, target_test)
        return score

    def aggregate(self, scores):
        return scores.median(dim='neuroid')



class CrossRegressedCorrelationCovariate:
    def __init__(self, regression, correlation, crossvalidation_kwargs=None):
        regression = regression or pls_regression()
        crossvalidation_defaults = dict(train_size=.9, test_size=None)
        crossvalidation_kwargs = {**crossvalidation_defaults, **(crossvalidation_kwargs or {})}

        self.cross_validation = CrossValidation(expecting_coveriate=True, **crossvalidation_kwargs)
        self.regression = regression
        self.correlation = correlation

    def __call__(self, source, covariate, target):
        return self.cross_validation(source, covariate, target, apply=self.apply, aggregate=self.aggregate)

    def apply(self, source_train, covariate_train, target_train, source_test, covariate_test, target_test):
        self.regression.fit(source_train, covariate_train, target_train)
        prediction = self.regression.predict(source_test, covariate_test)
        score = self.correlation(prediction, target_test)
        return score

    def aggregate(self, scores):
        return scores.median(dim='neuroid')


class CovariateRegression():
    def __init__(self, covariate_control = False, scaler_kwargs=None, pca_kwargs=None, regression_kwargs=None):
        self.covariate_control = covariate_control
        self.scaler_kwargs = scaler_kwargs or {}
        self.pca_kwargs = pca_kwargs or {}
        self.regression_kwargs = regression_kwargs or {}
        self.scaler_x = StandardScaler(**self.scaler_kwargs)
        self.scaler_cov = StandardScaler(**self.scaler_kwargs)
        self.scaler_y = StandardScaler(**self.scaler_kwargs)
        self.pca_x = PCA(**self.pca_kwargs)
        self.pca_cov = PCA(**self.pca_kwargs)
        self.control_regression = LinearRegression(**self.regression_kwargs)
        self.main_regression = LinearRegression(**self.regression_kwargs)

    def _get_residuals(self, X, X_cov, fit=True):
        # Residuals
        if fit:
            self.control_regression.fit(X_cov, X)
        X = X - self.control_regression.predict(X_cov)

        return X

    def fit(self, X, X_cov, Y):
        # Center/scale
        X.values = self.scaler_x.fit_transform(X)
        X_cov.values = self.scaler_cov.fit_transform(X_cov)
        Y = self.scaler_y.fit_transform(Y)

        # PCA
        X = self.pca_x.fit_transform(X)
        X_cov = self.pca_cov.fit_transform(X_cov)

        # Residuals
        if self.covariate_control:
            X = self._get_residuals(X, X_cov, fit=True)

        self.main_regression.fit(X, Y)

    def predict(self, X, X_cov):
        # Center/scale
        X.values = self.scaler_x.transform(X)
        X_cov.values = self.scaler_cov.transform(X_cov)

        # PCA
        X = self.pca_x.transform(X)
        X_cov = self.pca_cov.transform(X_cov)

        # Residuals
        if self.covariate_control:
            X = self._get_residuals(X, X_cov, fit=False)

        Ypred = self.main_regression.predict(X)
        return self.scaler_y.inverse_transform(Ypred)  # is this wise?


class CovariatePLS():
    def __init__(self, covariate_control = False, regression_kwargs=None):
        self.covariate_control = covariate_control
        self.regression_kwargs = regression_kwargs or {}
        self.control_regression = PLSRegression(**self.regression_kwargs)
        self.main_regression = PLSRegression(**self.regression_kwargs)

    def _get_residuals(self, X, X_cov, fit=True):
        # Residuals
        if fit:
            self.control_regression.fit(X_cov, X)
        X = X - self.control_regression.predict(X_cov)

        return X

    def fit(self, X, X_cov, Y):

        # Residuals
        if self.covariate_control:
            X = self._get_residuals(X, X_cov, fit=True)

        self.main_regression.fit(X, Y)

    def predict(self, X, X_cov):

        # Residuals
        if self.covariate_control:
            X = self._get_residuals(X, X_cov, fit=False)

        Ypred = self.main_regression.predict(X)
        return Ypred



def covariate_regression(covariate_control=False,scaler_kwargs=None, pca_kwargs=None, regression_kwargs=None, xarray_kwargs=None):
    scaler_defaults = dict(with_std=False)
    pca_defaults = dict(n_components=25)
    scaler_kwargs = {**scaler_defaults, **(scaler_kwargs or {})}
    pca_kwargs = {**pca_defaults, **(pca_kwargs or {})}
    regression_kwargs = regression_kwargs or {}
    regression = CovariateRegression(covariate_control = covariate_control,
                                       scaler_kwargs=scaler_kwargs,
                                       pca_kwargs=pca_kwargs,
                                       regression_kwargs=regression_kwargs)
    xarray_kwargs = xarray_kwargs or {}
    regression = XarrayCovariateRegression(regression, **xarray_kwargs)
    return regression

def covariate_pls(covariate_control=False, regression_kwargs=None, xarray_kwargs=None):
    regression_defaults = dict(n_components=25, scale=False)
    regression_kwargs = {**regression_defaults, **(regression_kwargs or {})}
    regression = CovariatePLS(covariate_control = covariate_control, regression_kwargs=regression_kwargs)
    xarray_kwargs = xarray_kwargs or {}
    regression = XarrayCovariateRegression(regression, **xarray_kwargs)
    return regression


class XarrayCovariateRegression:
    """
    Adds alignment-checking, un- and re-packaging, and comparison functionality to a regression.
    """

    def __init__(self, regression, expected_dims=Defaults.expected_dims, neuroid_dim=Defaults.neuroid_dim,
                 neuroid_coord=Defaults.neuroid_coord, stimulus_coord=Defaults.stimulus_coord):
        self._regression = regression
        self._expected_dims = expected_dims
        self._neuroid_dim = neuroid_dim
        self._neuroid_coord = neuroid_coord
        self._stimulus_coord = stimulus_coord
        self._target_neuroid_values = None

    def fit(self, source, covariate, target):
        source, covariate, target = self._align(source), self._align(covariate), self._align(target)
        source, covariate, target = source.sortby(self._stimulus_coord), covariate.sortby(self._stimulus_coord), target.sortby(self._stimulus_coord)

        self._regression.fit(source, covariate, target)

        self._target_neuroid_values = {}
        for name, dims, values in walk_coords(target):
            if self._neuroid_dim in dims:
                assert array_is_element(dims, self._neuroid_dim)
                self._target_neuroid_values[name] = values

    def predict(self, source, covariate):
        source, covariate = self._align(source), self._align(covariate)
        source, covariate = source.sortby(self._stimulus_coord), covariate.sortby(self._stimulus_coord)
        predicted_values = self._regression.predict(source, covariate)
        prediction = self._package_prediction(predicted_values, source=source)
        return prediction

    def _package_prediction(self, predicted_values, source):
        coords = {coord: (dims, values) for coord, dims, values in walk_coords(source)
                  if not array_is_element(dims, self._neuroid_dim)}
        # re-package neuroid coords
        dims = source.dims
        # if there is only one neuroid coordinate, it would get discarded and the dimension would be used as coordinate.
        # to avoid this, we can build the assembly first and then stack on the neuroid dimension.
        neuroid_level_dim = None
        if len(self._target_neuroid_values) == 1:  # extract single key: https://stackoverflow.com/a/20145927/2225200
            (neuroid_level_dim, _), = self._target_neuroid_values.items()
            dims = [dim if dim != self._neuroid_dim else neuroid_level_dim for dim in dims]
        for target_coord, target_value in self._target_neuroid_values.items():
            # this might overwrite values which is okay
            coords[target_coord] = (neuroid_level_dim or self._neuroid_dim), target_value
        prediction = NeuroidAssembly(predicted_values, coords=coords, dims=dims)
        if neuroid_level_dim:
            prediction = prediction.stack(**{self._neuroid_dim: [neuroid_level_dim]})

        return prediction

    def _align(self, assembly):
        assert set(assembly.dims) == set(self._expected_dims), \
            f"Expected {set(self._expected_dims)}, but got {set(assembly.dims)}"
        return assembly.transpose(*self._expected_dims)
