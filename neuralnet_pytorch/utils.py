import json

import numpy as np
import torch as T
import torch.nn.functional as F
import yaml
import abc
import threading
from queue import Queue
from scipy.stats import truncnorm
from PIL import Image
from slackclient import SlackClient

cuda_available = T.cuda.is_available()

__all__ = ['cuda_available', 'DataLoader']


def validate(func):
    """make sure output shape is a list of ints"""

    def func_wrapper(self):
        if func(self) is None:
            return None

        shape = [None if x is None else None if np.isnan(x) else x for x in func(self)]
        out = [int(x) if x is not None else x for x in shape]
        return tuple(out)

    return func_wrapper


class ConfigParser(object):
    def __init__(self, config_file, type='json', **kwargs):
        super(ConfigParser, self).__init__()
        self.config_file = config_file
        self.config = self.load_configuration()
        self.type = type

    def load_configuration(self):
        try:
            with open(self.config_file) as f:
                data = json.load(f) if type == 'json' else yaml.load(f)
            print('Config file loaded successfully')
        except:
            raise NameError('Unable to open config file!!!')
        return data


class ThreadsafeIter:
    """Takes an iterator/generator and makes it thread-safe by
    serializing call to the `next` method of given iterator/generator.
    """
    def __init__(self, it):
        self.it = it
        self.lock = threading.Lock()

    def __iter__(self):
        return self

    def __next__(self):
        with self.lock:
            return self.it.__next__()


def threadsafe_generator(generator):
    """A decorator that takes a generator function and makes it thread-safe.
    """
    def safe_generator(*agrs, **kwargs):
        return ThreadsafeIter(generator(*agrs, **kwargs))
    return safe_generator


class DataLoader(metaclass=abc.ABCMeta):
    def __init__(self, dataset, batch_size, shuffle=False, num_workers=10, collate_fn=None, augmentation=None,
                 apply_augmentation_to=(0,), num_cached=10, **kwargs):
        """
                A lightweight data loader. Works comparably to Pytorch's Dataloader when the workload is light, but it
        initializes much faster. It can be a drop-in for Pytorch's Dataloader.
        Usage: Subclass this class and define the load_data method. Optionally, generator can also be defined to specify
        how to load.

        :param dataset: an instance of Pytorch's Dataset
        :param batch_size: batch size
        :param shuffle: whether to shuffle in each iteration
        :param augmentation: an instance of Pytorch's transform classes. Applicable to 4D image tensor
        :param apply_augmentation_to: augmentation is only applied to the elements in a batch whose indices is specified
            here
        :param return_epoch: whether to return epoch or iteration number. Default iteration
        :param infinite: whether to run inifinitely
        :param num_cached: number of batches to be cached
        :param num_workers: number of threads to be used
        :param collate_fn: function to specify how a batch is loaded
        :param kwargs:
        """
        self.dataset = dataset
        self.batch_size = batch_size
        self.shuffle = shuffle
        self.augmentation = augmentation
        self.num_cached = num_cached
        self.apply_to = apply_augmentation_to
        self.num_workers = num_workers
        self.collate_fn = collate_fn
        self.kwargs = kwargs
        self.batches = None
        self.indices = None
        self.num_batches = len(self.dataset) // self.batch_size

    def __iter__(self):
        self.batches = self._get_batches()
        return self

    def __next__(self):
        if self.batches is None:
            self.batches = self._get_batches()
        return self.batches.__next__()

    def _augment_minibatches(self, minibatches):
        for batch in minibatches:
            if isinstance(batch, (list, tuple)):
                assert isinstance(self.apply_to, (list, tuple)), \
                    'Expect a list of indices to which augmentation is applied. Got %s.' % type(self.apply_to)

                for idx in self.apply_to:
                    batch[idx] = [np.transpose(self.augmentation(
                        Image.fromarray((np.transpose(img, (1, 2, 0)) * 255.).astype('uint8'), 'RGB')), (2, 0, 1)) for
                                  img in batch[idx]]
                    batch[idx] = np.stack(batch[idx]).astype('float32') / 255.
            else:
                batch = [np.transpose(self.augmentation(
                        Image.fromarray((np.transpose(img, (1, 2, 0)) * 255.).astype('uint8'), 'RGB')), (2, 0, 1)) for
                                  img in batch]
                batch = np.stack(batch).astype('float32') / 255.
            yield batch.astype

    def _get_batches(self):
        batches = self._generator()
        if self.augmentation:
            batches = self._augment_minibatches(batches)

        batches = self._generate_in_background(batches)
        for it, batch in enumerate(batches):
            batch = T.tensor(batch) if isinstance(batch, np.ndarray) else [T.tensor(b) for b in batch]
            yield batch

    def _generate_in_background(self, generator):
        """
        Runs a generator in a background thread, caching up to `num_cached` items.
        """
        generator = ThreadsafeIter(generator)
        queue = Queue(maxsize=self.num_cached)

        # define producer (putting items into queue)
        def producer():
            for item in generator:
                queue.put(item)
            queue.join()
            queue.put(None)

        # start producer (in a background thread)
        for i in range(self.num_workers):
            thread = threading.Thread(target=producer, daemon=True)
            thread.start()

        # run as consumer (read items from queue, in current thread)
        while True:
            item = queue.get()
            if item is None:
                break
            yield item
            queue.task_done()

    def _generator(self):
        assert isinstance(self.dataset[0],
                          (list, tuple, np.ndarray)), 'Dataset should consist of lists/tuples or Numpy ndarray'
        self.indices = np.arange(len(self.dataset))
        if self.shuffle:
            np.random.shuffle(self.indices)

        for i in range(self.num_batches):
            slice_ = self.indices[i * self.batch_size:(i + 1) * self.batch_size]
            batch = [self.dataset[s] for s in slice_]

            if isinstance(batch, np.ndarray):
                batch = np.stack(batch, 0)

            if isinstance(batch, (list, tuple)):
                batch = tuple([np.stack(b) for b in zip(*batch)]) if self.collate_fn is None else self.collate_fn(batch)

            yield batch


def truncated_normal(tensor, a=-1, b=1, mean=0., std=1.):
    values = truncnorm.rvs(a, b, loc=mean, scale=std, size=list(tensor.shape))
    with T.no_grad():
        tensor.data.copy_(T.tensor(values))


def lrelu(x, **kwargs):
    alpha = kwargs.get('alpha', 0.2)
    return F.leaky_relu(x, alpha, False)


def rgb2gray(img):
    if img.ndimension() != 4:
        raise ValueError('Input images must have four dimensions, not %d' % img.ndimension())
    return (0.299 * img[:, 0] + 0.587 * img[:, 1] + 0.114 * img[:, 2]).unsqueeze(1)


def rgb2ycbcr(img):
    if img.ndimension() != 4:
        raise ValueError('Input images must have four dimensions, not %d' % img.ndimension())
    Y = 0. + .299 * img[:, 0] + .587 * img[:, 1] + .114 * img[:, 2]
    Cb = 128. - .169 * img[:, 0] - .331 * img[:, 1] + .5 * img[:, 2]
    Cr = 128. + .5 * img[:, 0] - .419 * img[:, 1] - .081 * img[:, 2]
    return T.cat((Y.unsqueeze(1), Cb.unsqueeze(1), Cr.unsqueeze(1)), 1)


def ycbcr2rgb(img):
    if img.ndimension() != 4:
        raise ValueError('Input images must have four dimensions, not %d' % img.ndimension())
    R = img[:, 0] + 1.4 * (img[:, 2] - 128.)
    G = img[:, 0] - .343 * (img[:, 1] - 128.) - .711 * (img[:, 2] - 128.)
    B = img[:, 0] + 1.765 * (img[:, 1] - 128.)
    return T.cat((R.unsqueeze(1), G.unsqueeze(1), B.unsqueeze(1)), 1)


def batch_get_value(params):
    return tuple([to_numpy(p) for p in params])


def batch_set_value(params, values):
    for p, v in zip(params, values):
        p.data.copy_(T.from_numpy(v))


def to_numpy(x):
    return x.cpu().detach().data.numpy()


def to_cuda(x):
    return T.from_numpy(x).cuda()


def bulk_to_numpy(xs):
    return tuple([to_numpy(x) for x in xs])


def bulk_to_cuda(xs):
    return tuple([to_cuda(x) for x in xs])


def dimshuffle(x, pattern):
    assert isinstance(pattern, (list, tuple)), 'pattern must be a list/tuple'
    no_expand_pattern = [x for x in pattern if x != 'x']
    y = x.permute(*no_expand_pattern)
    shape = list(y.shape)
    for idx, e in enumerate(pattern):
        if e == 'x':
            shape.insert(idx, 1)
    return y.view(*shape)


def shape_padleft(x, n_ones=1):
    pattern = ('x',) * n_ones + tuple(range(x.ndimension()))
    return dimshuffle(x, pattern)


def shape_padright(x, n_ones=1):
    pattern = tuple(range(x.ndimension())) + ('x',) * n_ones
    return dimshuffle(x, pattern)


def ravel_index(indices, shape):
    assert len(indices) == len(shape), 'Indices and shape must have the same length'
    shape = T.tensor(shape)
    if cuda_available:
        shape = shape.cuda()
    return sum([T.tensor(indices[i], dtype=shape.dtype) * T.prod(shape[i + 1:]) for i in range(len(shape))])


def smooth(x, beta=.9, window='hanning'):
    """smooth the data using a window with requested size.

    This method is based on the convolution of a scaled window with the signal.
    The signal is prepared by introducing reflected copies of the signal
    (with the window size) in both ends so that transient parts are minimized
    in the begining and end part of the output signal.

    input:
        x: the input signal
        beta: the weighted moving average coeff. window length = 1 / (1 - beta)
        window: the type of window from 'flat', 'hanning', 'hamming', 'bartlett', 'blackman'
            flat window will produce a moving average smoothing.

    output:
        the smoothed signal

    example:

    t=linspace(-2,2,0.1)
    x=sin(t)+randn(len(t))*0.1
    y=smooth(x)

    see also:

    numpy.hanning, numpy.hamming, numpy.bartlett, numpy.blackman, numpy.convolve
    scipy.signal.lfilter
    """
    x = np.array(x)
    assert x.ndim == 1, 'smooth only accepts 1 dimension arrays'
    assert 0 < beta < 1, 'Input vector needs to be bigger than window size'

    window_len = int(1 / (1 - beta))
    if window_len < 3 or x.shape[0] < window_len:
        return x

    s = np.r_[x[window_len - 1:0:-1], x, x[-2:-window_len - 1:-1]]
    if isinstance(window, str):
        assert window in ['flat', 'hanning', 'hamming', 'bartlett', 'blackman'], \
            'Window is on of \'flat\', \'hanning\', \'hamming\', \'bartlett\', \'blackman\''

        if window == 'flat':  # moving average
            w = np.ones(window_len, 'd')
        else:
            w = eval('np.' + window + '(window_len)')
    else:
        window = np.array(window)
        assert window.ndim == 1, 'Window must be a 1-dim array'
        w = window

    y = np.convolve(w / w.sum(), s, mode='valid')
    return y if y.shape[0] == x.shape[0] else y[(window_len // 2 - 1):-(window_len // 2)]


def batch_pairwise_dist(x, y):
    bs, num_points_x, points_dim = x.size()
    _, num_points_y, _ = y.size()
    xx = T.bmm(x, x.transpose(2, 1))
    yy = T.bmm(y, y.transpose(2, 1))
    zz = T.bmm(x, y.transpose(2, 1))

    if cuda_available:
        dtype = T.cuda.LongTensor
    else:
        dtype = T.LongTensor

    diag_ind_x = T.arange(0, num_points_x).type(dtype)
    diag_ind_y = T.arange(0, num_points_y).type(dtype)
    # brk()
    rx = xx[:, diag_ind_x, diag_ind_x].unsqueeze(1).expand_as(zz.transpose(2, 1))
    ry = yy[:, diag_ind_y, diag_ind_y].unsqueeze(1).expand_as(zz)
    P = (rx.transpose(2, 1) + ry - 2 * zz)
    return P


def time_cuda_module(f, *args, **kwargs):
    start = T.cuda.Event(enable_timing=True)
    end = T.cuda.Event(enable_timing=True)

    start.record()
    f(*args, **kwargs)
    end.record()

    # Waits for everything to finish running
    T.cuda.synchronize()

    total = start.elapsed_time(end)
    print('Took %fs' % total)
    return total


def slack_message(username, message, channel, token, **kwargs):
    sc = SlackClient(token)
    sc.api_call('chat.postMessage', channel=channel, text=message, username=username, **kwargs)


function = {'relu': lambda x, **kwargs: F.relu(x, False), 'linear': lambda x, **kwargs: x, None: lambda x, **kwargs: x,
            'lrelu': lambda x, **kwargs: lrelu(x, **kwargs), 'tanh': lambda x, **kwargs: T.tanh(x),
            'sigmoid': lambda x, **kwargs: T.sigmoid(x), 'elu': lambda x, **kwargs: F.elu(x, **kwargs),
            'softmax': lambda x, **kwargs: F.softmax(x, **kwargs)}
