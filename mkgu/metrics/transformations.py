import itertools
import logging
from collections import OrderedDict

import numpy as np
from sklearn.model_selection import StratifiedShuffleSplit, ShuffleSplit

from mkgu.assemblies import merge_data_arrays, DataAssembly
from mkgu.utils import fullname


def enumerate_done(values):
    for i, val in enumerate(values):
        done = i == len(values) - 1
        yield i, val, done


def apply_transformations(*args, transformations, computor):
    """
    Start from first transformation, pass all of the transformed values into second transformation and so forth.
    When no transformations are left, apply the computor on each of the transformed values.
    When values are coming back, go the reverse way:
    collect all computed values, send them to the last transformation.
    Once the last transformation is complete, return its result to the second-last transformation and so forth.

    Note that transformations are expected to yield a transformed value,
    receive the result of computing that transformation,
    and signal whether all transformations are done.
    When the last transformation is done, the generator ought to yield the combined result.
    """

    def recurse(generator, generators):
        for vals in generator:
            if len(generators) > 0:
                gen = generators[0]
                gen = gen(*vals)
                y = recurse(gen, generators[1:])
            else:
                y = computor(*vals)
            done = generator.send(y)
            if done:
                break
        result = next(generator)
        return result

    result = recurse(transformations[0](*args), transformations[1:])
    return result


class Transformation(object):
    """
    Transforms an incoming assembly into parts/combinations thereof,
    yields them for further processing,
    and packages the results back together.
    """

    def _get_result(self, *args, done):
        """
        Yields the `*args` for further processing by coroutines
        and waits for the result to be sent back.
        :param args: transformed values
        :param bool done: whether this is the last transformation and the next `yield` is the combined result
        :return: the result from processing by the coroutine
        """
        result = yield args  # yield the values to coroutine
        yield done  # wait for coroutine to send back similarity and inform whether result is ready to be returned
        return result


class Alignment(Transformation):
    def __init__(self):
        self._logger = logging.getLogger(fullname(self))

    def __call__(self, source_assembly, target_assembly):
        self._logger.debug("Aligning")
        source_assembly = self.align(source_assembly, target_assembly)
        self._logger.debug("Sorting")
        source_assembly, target_assembly = self.sort(source_assembly), self.sort(target_assembly)
        result = yield from self._get_result(source_assembly, target_assembly, done=True)
        yield result

    def align(self, source_assembly, target_assembly, subset_dim='presentation'):
        dimensions = ['presentation', 'neuroid']
        dimensions += list(set(source_assembly.dims) - set(dimensions))
        source_assembly = source_assembly.transpose(*dimensions)
        return subset(source_assembly, target_assembly, subset_dims=[subset_dim])  # , repeat=True)

    def sort(self, assembly):
        return assembly.sortby('image_id')


class CartesianProduct(Transformation):
    """
    Splits an incoming assembly along all dimensions that similarity is not computed over
    as well as along dividing coords that denote separate parts of the assembly.
    """

    class Defaults:
        similarity_dims = 'presentation', 'neuroid'
        dividing_coords = 'region',

    def __init__(self,
                 similarity_dims=Defaults.similarity_dims, dividing_coord_names=Defaults.dividing_coords,
                 dividing_coord_names_source=(), dividing_coord_names_target=()):
        super(CartesianProduct, self).__init__()
        self._similarity_dims = similarity_dims
        self._dividing_coord_names = dividing_coord_names
        self._dividing_coord_names_source = dividing_coord_names_source
        self._dividing_coord_names_target = dividing_coord_names_target

        self._logger = logging.getLogger(fullname(self))

    def __call__(self, source_assembly, target_assembly):
        """
        :param mkgu.assemblies.NeuroidAssembly source_assembly:
        :param mkgu.assemblies.NeuroidAssembly target_assembly:
        :return: mkgu.assemblies.DataAssembly
        """

        # divide data along dividing coords and non-central dimensions,
        # i.e. dimensions that the metric is not computed over
        def dividing_selections(assembly, dividing_coord_names):
            dividing_coords = [dim for dim in assembly.dims if dim not in self._similarity_dims] \
                              + [coord for coord in dividing_coord_names if hasattr(assembly, coord)]
            choices = {coord: np.unique(assembly[coord]) for coord in dividing_coords}
            combinations = [dict(zip(choices, values)) for values in itertools.product(*choices.values())]
            return combinations

        dividers_source = dividing_selections(source_assembly,
                                              self._dividing_coord_names + tuple(self._dividing_coord_names_source))
        dividers_target = dividing_selections(target_assembly,
                                              self._dividing_coord_names + tuple(self._dividing_coord_names_target))
        # run all dividing combinations or use the assemblies themselves if no dividers
        divider_combinations = list(itertools.product(dividers_source, dividers_target)) or [({}, {})]
        similarities = []
        for i, (div_src, div_tgt), done in enumerate_done(divider_combinations):
            self._logger.debug("dividers {}/{}: {} | {}".format(i + 1, len(divider_combinations), div_src, div_tgt))
            source_div, target_div = source_assembly.multisel(**div_src), target_assembly.multisel(**div_tgt)
            similarity = yield from self._get_result(source_div, target_div, done=done)

            divider_coords = self.merge_dividers(div_src, div_tgt)
            for coord_name, coord_value in divider_coords.items():
                similarity[coord_name] = coord_value
            similarities.append(similarity)
        assert all(similarity.shape == similarities[0].shape for similarity in similarities[1:])  # all shapes equal

        # re-shape into dividing dimensions and split
        assembly_dims = source_assembly.dims + target_assembly.dims + tuple(self._dividing_coord_names) \
                        + tuple(self._dividing_coord_names_source) + tuple(self._dividing_coord_names_target) \
                        + ('split',)
        similarities = [expand(similarity, assembly_dims) for similarity in similarities]
        similarities = merge_data_arrays(similarities)
        yield similarities

    def merge_dividers(self, div_left, div_right):
        coords = list(div_left.keys()) + list(div_right.keys())
        duplicates = [coord for coord in coords if coords.count(coord) > 1]
        coords_left = {coord if coord not in duplicates else coord + '_left': coord_value
                       for coord, coord_value in div_left.items()}
        coords_right = {coord if coord not in duplicates else coord + '_right': coord_value
                        for coord, coord_value in div_right.items()}
        return {**coords_left, **coords_right}


class CrossValidation(Transformation):
    class Defaults:
        cross_validation_splits = 10
        cross_validation_data_ratio = .9
        cross_validation_dim = 'image_id'
        stratification_coord = 'object_name'  # cross-validation across images, balancing objects

    def __init__(self,
                 cross_validation_splits=Defaults.cross_validation_splits,
                 cross_validation_data_ratio=Defaults.cross_validation_data_ratio,
                 cross_validation_dim=Defaults.cross_validation_dim,
                 stratification_coord=Defaults.stratification_coord):
        super().__init__()
        self._stratified_split = StratifiedShuffleSplit(
            n_splits=cross_validation_splits, train_size=cross_validation_data_ratio)
        self._shuffle_split = ShuffleSplit(
            n_splits=cross_validation_splits, train_size=cross_validation_data_ratio)
        self._cross_validation_dim = cross_validation_dim
        self._stratification_coord = stratification_coord

        self._logger = logging.getLogger(fullname(self))

    def __call__(self, source_assembly, target_assembly):
        assert all(source_assembly[self._cross_validation_dim].values ==
                   target_assembly[self._cross_validation_dim].values)

        cross_validation_values = target_assembly[self._cross_validation_dim]
        unique_cross_validation_values = np.unique(source_assembly[self._cross_validation_dim])
        if hasattr(target_assembly, self._stratification_coord):
            assert hasattr(source_assembly, self._stratification_coord)
            assert all(source_assembly[self._stratification_coord].values ==
                       target_assembly[self._stratification_coord].values)
            splits = list(self._stratified_split.split(np.zeros(len(unique_cross_validation_values)),
                                                       source_assembly[self._stratification_coord].values))
        else:
            self._logger.warning("Stratification coord '{}' not found in assembly "
                                 "- falling back to un-stratified splits".format(self._stratification_coord))
            splits = list(self._shuffle_split.split(np.zeros(len(unique_cross_validation_values))))

        split_scores = {}
        for split_iterator, (train_indices, test_indices), done in enumerate_done(splits):
            self._logger.debug("split {}/{}".format(split_iterator + 1, len(splits)))
            train_values, test_values = cross_validation_values[train_indices], cross_validation_values[test_indices]
            train_source = subset(source_assembly, train_values)
            train_target = subset(target_assembly, train_values)
            test_source = subset(source_assembly, test_values)
            test_target = subset(target_assembly, test_values)

            split_score = yield from self._get_result(train_source, train_target, test_source, test_target, done=done)
            split_scores[split_iterator] = split_score

        coords = {'split': list(split_scores.keys())}
        split_scores = DataAssembly(list(split_scores.values()), coords=coords, dims=['split'])
        yield split_scores


def subset(source_assembly, target_assembly, subset_dims=None, dims_must_match=True, repeat=False):
    """
    :param subset_dims: either dimensions, then all its levels will be used or levels right away
    :param dims_must_match:
    :return:
    """
    subset_dims = subset_dims or target_assembly.dims
    for dim in subset_dims:
        assert dim in target_assembly.dims
        assert dim in source_assembly.dims
        # we assume here that it does not matter if all levels are present in the source assembly
        # as long as there is at least one level that we can select over
        levels = target_assembly[dim].variable.level_names or [dim]
        assert any(hasattr(source_assembly, level) for level in levels)
        for level in levels:
            if not hasattr(source_assembly, level):
                continue
            target_values = target_assembly[level].values
            source_values = source_assembly[level].values
            if repeat:
                indexer = index_efficient(source_values, target_values)
                dim_indexes = {_dim: slice(None) if _dim != dim else indexer for _dim in source_assembly.dims}
            else:
                level_values = target_assembly[level].values
                indexer = np.array([val in level_values for val in source_assembly[level].values])
                dim_indexes = {_dim: slice(None) if _dim != dim else np.where(indexer)[0] for _dim in
                               source_assembly.dims}
            source_assembly = source_assembly.isel(**dim_indexes)
        if dims_must_match:
            # dims match up after selection. cannot compare exact equality due to potentially missing levels
            assert len(target_assembly[dim]) == len(source_assembly[dim])
    return source_assembly


def index_efficient(source_values, target_values):
    source_sort_indeces, target_sort_indeces = np.argsort(source_values), np.argsort(target_values)
    source_values, target_values = source_values[source_sort_indeces], target_values[target_sort_indeces]
    indexer = []
    source_index, target_index = 0, 0
    while target_index < len(target_values) and source_index < len(source_values):
        if source_values[source_index] == target_values[target_index]:
            indexer.append(source_sort_indeces[source_index])
            target_index += 1
        elif source_values[source_index] < target_values[target_index]:
            source_index += 1
        else:  # source_values[source_index] > target_values[target_index]:
            target_index += 1
    return indexer


def expand(assembly, target_dims):
    def strip(coord):
        stripped_coord = coord
        if stripped_coord.endswith('_left'):
            stripped_coord = stripped_coord[:-len('_left')]
        if stripped_coord.endswith('_right'):
            stripped_coord = stripped_coord[:-len('_right')]
        return stripped_coord

    def reformat_coord_values(coord, dims, values):
        stripped_coord = strip(coord)

        if stripped_coord in target_dims and len(values.shape) == 0:
            values = np.array([values])
            dims = [coord]
        return dims, values

    coords = {coord: reformat_coord_values(coord, values.dims, values.values)
              for coord, values in assembly.coords.items()}
    dim_shapes = OrderedDict((coord, values[1].shape)
                             for coord, values in coords.items() if strip(coord) in target_dims)
    shape = [_shape for shape in dim_shapes.values() for _shape in shape]
    # prepare values for broadcasting by adding new dimensions
    values = assembly.values
    for _ in range(sum([dim not in assembly.dims for dim in dim_shapes])):
        values = values[:, np.newaxis]
    values = np.broadcast_to(values, shape)
    return DataAssembly(values, coords=coords, dims=list(dim_shapes.keys()))
