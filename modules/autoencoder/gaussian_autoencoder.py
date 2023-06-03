import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import pytorch_lightning as pl

from .modules import (
    TimePositionalEmbedding, EncodingBlock, DecodingBlock,
    ResidualBlock, SelfAttention
)

from ..loss.lpips import LPIPSWithDiscriminator

class Encoder(nn.Module):
    def __init__(
        self, in_channels, z_channels=4, pemb_dim=None, num_channels=128, channels_mult=[1, 2, 4, 4], 
        num_res_blocks=2, attn=None
        ) -> None:
        super().__init__()
        if attn is not None:
            assert channels_mult.__len__() == attn.__len__(), 'channels_mult and attn must have the same length'
            self.attn = attn
        else:
            self.attn = [False] * channels_mult.__len__()

        self.z_channels = z_channels
        self.channels_mult = [1, *channels_mult]
        self.attn = attn
        
        # architecture modules
        self.in_conv = nn.Conv2d(in_channels, num_channels, kernel_size=3, padding='same')
        self.enocoder = nn.ModuleList([
            EncodingBlock(
                in_channels=num_channels * self.channels_mult[idx],
                out_channels=num_channels * self.channels_mult[idx + 1],
                temb_dim=pemb_dim,
                num_blocks=num_res_blocks,
                attn=self.attn[idx],
                downsample=True if idx != self.channels_mult.__len__() - 2 else False
            ) for idx in range(self.channels_mult.__len__() - 1)
        ])
        bottleneck_channels = num_channels * self.channels_mult[-1]
        seeneck_channels = num_channels * self.channels_mult[-1]
        self.bottleneck_res_a = ResidualBlock(in_channels=bottleneck_channels, out_channels=bottleneck_channels, temb_dim=pemb_dim, groups=8)
        self.bottleneck_sa = SelfAttention(in_channels=bottleneck_channels, num_heads=8, head_dim=32, groups=8)
        self.bottleneck_res_b = ResidualBlock(in_channels=bottleneck_channels, out_channels=bottleneck_channels, temb_dim=pemb_dim, groups=8)
        self.out_conv = nn.Sequential(
            nn.GroupNorm(num_groups=8, num_channels=bottleneck_channels),
            nn.SiLU(),
            nn.Conv2d(in_channels=bottleneck_channels, out_channels=self.z_channels, kernel_size=3, padding=1)
        )
    
    def forward(self, x, pemb=None):
        x = self.in_conv(x)
        for encoder in self.enocoder:
            x = encoder(x, pemb)
        x = self.bottleneck_res_a(x, pemb)
        x = self.bottleneck_sa(x)
        x = self.bottleneck_res_b(x, pemb)
        x = self.out_conv(x)
        return x
    
class Decoder(nn.Module):
    def __init__(
        self, out_channels, z_channels, pemb_dim=None, num_channels=128, channels_mult=[1, 2, 4, 4],
        num_res_blocks=2, attn=None
        ) -> None:
        super().__init__()
        if attn is not None:
            assert channels_mult.__len__() == attn.__len__(), 'channels_mult and attn must have the same length'
            self.attn = list(reversed(attn))
        else: 
            self.attn = [False] * channels_mult.__len__()

        self.channels_mult = list(reversed([1, *channels_mult]))
        self.z_channels = z_channels
        
        # architecture modules
        bottleneck_channels = num_channels * self.channels_mult[0]
        self.in_conv = nn.Conv2d(self.z_channels, bottleneck_channels, kernel_size=3, padding='same')
        self.bottleneck_res_a = ResidualBlock(in_channels=bottleneck_channels, out_channels=bottleneck_channels, temb_dim=pemb_dim, groups=8)
        self.bottleneck_sa = SelfAttention(in_channels=bottleneck_channels, num_heads=8, head_dim=32, groups=8)
        self.bottleneck_res_b = ResidualBlock(in_channels=bottleneck_channels, out_channels=bottleneck_channels, temb_dim=pemb_dim, groups=8)

        self.decoder = nn.ModuleList([
            DecodingBlock(
                in_channels=num_channels * self.channels_mult[idx],
                out_channels=num_channels * self.channels_mult[idx + 1],
                temb_dim=pemb_dim,
                num_blocks=num_res_blocks,
                attn=self.attn[idx],
                upsample=True if idx != 0 else False
            ) for idx in range(self.channels_mult.__len__() - 1)
        ])
        
        self.out_conv = nn.Sequential(
            nn.GroupNorm(num_groups=8, num_channels=num_channels),
            nn.SiLU(),
            nn.Conv2d(in_channels=num_channels, out_channels=out_channels, kernel_size=3, padding=1)
        )
        
    def forward(self, x, pemb=None):
        x = self.in_conv(x)
        x = self.bottleneck_res_a(x, pemb)
        x = self.bottleneck_sa(x)
        x = self.bottleneck_res_b(x, pemb)
        for decoder in self.decoder:
            x = decoder(x, pemb)
        x = self.out_conv(x)
        return x
    
    
class GaussianAutoencoder(pl.LightningModule):
    def __init__(self, 
        input_shape=[2, 128, 128],
        embed_dim=2, 
        z_channels=4, 
        z_double=True,
        pemb_dim=128,
        T=64, 
        num_channels=128, 
        channels_mult=[1, 2, 4, 4], 
        num_res_blocks=2, 
        attn=[],
        learning_rate=1e-5,
        lr_d_factor=1.,
        **kwargs
    ) -> None:
        super().__init__()
        assert z_double == True, 'z_double must be True for GaussianAutoencoder'
        if attn.__len__() > 0:
            assert channels_mult.__len__() == attn.__len__(), 'channels_mult and attn must have the same length'
            self.attn = attn
        else:
            self.attn = [False] * channels_mult.__len__()

        self.z_channels = z_channels
        self.learning_rate = learning_rate
        self.lr_d_factor = lr_d_factor
        in_channels = out_channels = input_shape[0]
        
        # architecture modules
        self.positional_encoder = nn.Sequential(
            TimePositionalEmbedding(dimension=pemb_dim, T=T, device='cuda'),
            nn.Linear(pemb_dim, 128 * 4),
            nn.GELU(),
            nn.Linear(128 * 4, pemb_dim)
        )
        self.encoder = Encoder(in_channels, 2 * z_channels, pemb_dim, num_channels, channels_mult, num_res_blocks, self.attn)
        self.decoder = Decoder(out_channels, z_channels, pemb_dim, num_channels, channels_mult, num_res_blocks, self.attn)
        self.quant_conv = nn.Conv2d(2 * z_channels, 2 * embed_dim, kernel_size=1)
        self.post_quant_conv = nn.Conv2d(embed_dim, z_channels, kernel_size=1)

        # loss function
        self.loss = LPIPSWithDiscriminator(**kwargs['loss'])
        
        # TODO: Add EMA
        
        # pytorch lightining states
        self.automatic_optimization = False
        self.save_hyperparameters()
        
    def forward(self, x, pos, sample_posterior=True):
        pemb = self.positional_encoder(pos)
        posterior = self.encode(x, pemb)
        if sample_posterior:
            z = posterior.sample()
        else:
            z = posterior.mode()
        x = self.decode(z, pemb)
        return torch.tanh(x), z, posterior
    
    def encode(self, x, pemb):
        h = self.encoder(x, pemb)
        moments = self.quant_conv(h)
        posterior = DiagonalGaussianDistribution(moments)
        return posterior
    
    def decode(self, z, pemb):
        z = self.post_quant_conv(z)
        x = self.decoder(z, pemb)
        return x
    
    def encode_position(self, position):
        return self.positional_encoder(position)
    
    def on_train_batch_end(self, outputs, batch, batch_idx: int) -> None:
        pass # TODO: EMA
    
    def training_step(self, batch, batch_idx):
        # optimizers & schedulers
        ae_opt, disc_opt = self.optimizers()
        ae_scheduler, disc_scheduler = self.lr_schedulers()

        x, pos = batch
        x, pos = x.type(torch.float16), pos.type(torch.long)
        
        x_hat, z, posterior = self.forward(x, pos, sample_posterior=True)

        ########################
        # Optimize Autoencoder #
        ########################
        ae_loss, ae_log = self.loss.autoencoder_loss(x, x_hat, z, posterior, self.global_step, last_layer=self.decoder.out_conv[-1].weight)
        ae_opt.zero_grad(set_to_none=True)
        self.manual_backward(ae_loss)
        ae_opt.step()
        ae_scheduler.step()

        ##########################
        # Optimize Discriminator #
        ##########################
        disc_loss, disc_log = self.loss.discriminator_loss(x, x_hat, self.global_step)
        disc_opt.zero_grad(set_to_none=True)
        self.manual_backward(disc_loss)
        disc_opt.step()
        disc_scheduler.step()

        # logging
        self.log_dict(ae_log, on_step=True, on_epoch=True, prog_bar=True, logger=True)
        self.log_dict(disc_log, on_step=True, on_epoch=True, prog_bar=False, logger=True)

    def configure_optimizers(self):
        ae_opt = torch.optim.AdamW(list(self.encoder.parameters()) + 
                                   list(self.decoder.parameters()) + 
                                   list(self.positional_encoder.parameters()) +
                                   list(self.quant_conv.parameters()) +
                                   list(self.post_quant_conv.parameters()),
                                   lr=self.learning_rate, weight_decay=1e-6, betas=(0.5, 0.9))
        disc_opt = torch.optim.AdamW(list(self.loss.discriminator.parameters()), 
                                     lr=self.learning_rate * self.lr_d_factor, weight_decay=1e-6, betas=(0.5, 0.9))
        
        schedulers = [
            {
                'scheduler': torch.optim.lr_scheduler.CosineAnnealingLR(
                    ae_opt, T_max=self.trainer.max_steps, eta_min=1e-9, last_epoch=-1
                ),
                'interval': 'step',
                'frequency': 1
            },
            {
                'scheduler': torch.optim.lr_scheduler.CosineAnnealingLR(
                    disc_opt, T_max=self.trainer.max_steps, eta_min=1e-8, last_epoch=-1
                ),
                'interval': 'step',
                'frequency': 1
            }
        ]

        return [ae_opt, disc_opt], schedulers

        
class DiagonalGaussianDistribution(object):
    def __init__(self, parameters, deterministic=False):
        self.parameters = parameters
        self.mean, self.logvar = torch.chunk(parameters, 2, dim=1)
        self.logvar = torch.clamp(self.logvar, -30.0, 20.0)
        self.deterministic = deterministic
        self.std = torch.exp(0.5 * self.logvar)
        self.var = torch.exp(self.logvar)
        if self.deterministic:
            self.var = self.std = torch.zeros_like(self.mean).to(device=self.parameters.device)

    def sample(self):
        x = self.mean + self.std * torch.randn(self.mean.shape).to(device=self.parameters.device)
        return x

    def kl(self, other=None):
        if self.deterministic:
            return torch.Tensor([0.])
        else:
            if other is None:
                return 0.5 * torch.sum(torch.pow(self.mean, 2)
                                       + self.var - 1.0 - self.logvar,
                                       dim=[1, 2, 3])
            else:
                return 0.5 * torch.sum(
                    torch.pow(self.mean - other.mean, 2) / other.var
                    + self.var / other.var - 1.0 - self.logvar + other.logvar,
                    dim=[1, 2, 3])

    def nll(self, sample, dims=[1,2,3]):
        if self.deterministic:
            return torch.Tensor([0.])
        logtwopi = np.log(2.0 * np.pi)
        return 0.5 * torch.sum(
            logtwopi + self.logvar + torch.pow(sample - self.mean, 2) / self.var,
            dim=dims)

    def mode(self):
        return self.mean
