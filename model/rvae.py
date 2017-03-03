import numpy as np
import torch as t
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable

from .decoder import Decoder
from .encoder import Encoder

from selfModules.embedding import Embedding
from selfModules.selflinear import self_Linear

from utils.functional import kld_coef, parameters_allocation_check, fold


class RVAE(nn.Module):
    def __init__(self, params):
        super(RVAE, self).__init__()

        self.params = params

        self.embedding = Embedding(self.params, '../')

        self.encoder = Encoder(self.params)

        self.context_to_mu = self_Linear(self.params.latent_variable_size, self.params.latent_variable_size)
        self.context_to_logvar = self_Linear(self.params.latent_variable_size, self.params.latent_variable_size)

        self.decoder = Decoder(self.params)

    def forward(self, drop_prob,
                encoder_word_input=None, encoder_character_input=None,
                decoder_word_input=None,
                z=None, initial_state=None):
        """
        :param encoder_word_input: An tensor with shape of [batch_size, seq_len] of Long type
        :param encoder_character_input: An tensor with shape of [batch_size, seq_len, max_word_len] of Long type
        :param decoder_word_input: An tensor with shape of [batch_size, max_seq_len + 1] of Long type
        :param initial_state: initial state of decoder rnn in order to perform sampling

        :param drop_prob: probability of an element of context to be zeroed in sense of dropout

        :param z: context if sampling is performing

        :return: unnormalized logits of sentence words distribution probabilities
                    with shape of [batch_size, seq_len, word_vocab_size]
                 final rnn state with shape of [num_layers, batch_size, decoder_rnn_size]
        """

        assert parameters_allocation_check(self), \
            'Invalid CUDA options. Parameters should be allocated in the same memory'
        use_cuda = self.embedding.word_embed.weight.is_cuda

        assert z is None and fold(lambda acc, parameter: acc and parameter is not None,
                                  [encoder_word_input, encoder_character_input, decoder_word_input],
                                  True) \
            or (z is not None and decoder_word_input is not None), \
            "Invalid input. If z is None then encoder and decoder inputs should be passed as arguments"

        if z is None:
            ''' Get context from encoder and sample z ~ N(mu, std)
            '''
            [batch_size, _] = encoder_word_input.size()

            encoder_input = self.embedding(encoder_word_input, encoder_character_input)

            context = self.encoder(encoder_input)

            mu = self.context_to_mu(context)
            logvar = self.context_to_logvar(context)
            std = t.exp(0.5 * logvar)

            z = Variable(t.randn([batch_size, self.params.latent_variable_size]))
            if use_cuda:
                z = z.cuda()

            z = z * std + mu

            kld = (-0.5 * t.sum(logvar - t.pow(mu, 2) - t.exp(logvar) + 1, 1)).sum().squeeze()

            # z = F.dropout(z, p=drop_prob, training=True)

        else:
            kld = None

        decoder_input = self.embedding.word_embed(decoder_word_input)
        decoder_input = F.dropout(decoder_input, drop_prob)
        out, final_state = self.decoder(decoder_input, z, initial_state)
        return out, final_state, kld

    def learnable_paramters(self):
        # word_embedding is constant parameter thus it must be dropped from list of parameters for optimizer
        return [p for p in self.parameters() if p.requires_grad]

    def trainer(self, optimizer, batch_loader):
        def train(i, batch_size, use_cuda, dropout):

            input = batch_loader.next_batch(batch_size, 'train')
            input = [Variable(t.from_numpy(var)) for var in input]
            input = [var.long() for var in input]
            input = [var.cuda() if use_cuda else var for var in input]

            [encoder_word_input, encoder_character_input, decoder_word_input, target] = input

            [batch_size, seq_len] = decoder_word_input.size()

            logits, _, kld = self(dropout,
                                  encoder_word_input, encoder_character_input,
                                  decoder_word_input,
                                  z=None)

            logits = logits.view(-1, self.params.word_vocab_size)
            prediction = F.softmax(logits)
            target = target.view(-1, self.params.word_vocab_size)

            bce = F.binary_cross_entropy(prediction, target, size_average=False)/seq_len

            loss = (bce + kld_coef(i) * kld) / batch_size

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            return bce, kld, kld_coef(i)

        return train

    def sample(self, batch_loader, seq_len, seed, use_cuda):
        seed = Variable(t.from_numpy(seed).float())
        if use_cuda:
            seed = seed.cuda()

        decoder_word_input_np, _ = batch_loader.go_input(1)

        decoder_word_input = Variable(t.from_numpy(decoder_word_input_np).long())

        if use_cuda:
            decoder_word_input = decoder_word_input.cuda()

        result = ''

        initial_state = None

        for i in range(seq_len):
            logits, initial_state, _ = self(0., None, None,
                                            decoder_word_input,
                                            seed, initial_state)

            logits = logits.view(-1, self.params.word_vocab_size)
            prediction = F.softmax(logits)

            word = batch_loader.sample_word_from_distribution(prediction.data.cpu().numpy()[-1])

            if word == batch_loader.end_token:
                break

            result += ' ' + word

            decoder_word_input_np = np.array([[batch_loader.word_to_idx[word]]])

            decoder_word_input = Variable(t.from_numpy(decoder_word_input_np).long())

            if use_cuda:
                decoder_word_input = decoder_word_input.cuda()

        return result

