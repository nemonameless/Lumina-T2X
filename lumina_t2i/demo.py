import argparse
import builtins
import json
import multiprocessing as mp
import os
import socket
import traceback

import fairscale.nn.model_parallel.initialize as fs_init
import gradio as gr
import torch
import torch.distributed as dist
from torchvision.transforms.functional import to_pil_image

import models
from transport import create_transport, Sampler


class ModelFailure: pass


@torch.no_grad()
def model_main(args, master_port, rank, request_queue, response_queue, mp_barrier):
    # import here to avoid huggingface Tokenizer parallelism warnings
    from diffusers.models import AutoencoderKL
    from transformers import AutoModelForCausalLM, AutoTokenizer

    # override the default print function since the delay can be large for child process
    original_print = builtins.print

    # Redefine the print function with flush=True by default
    def print(*args, **kwargs):
        kwargs.setdefault('flush', True)
        original_print(*args, **kwargs)

    # Override the built-in print with the new version
    builtins.print = print

    os.environ["MASTER_PORT"] = str(master_port)
    os.environ["MASTER_ADDR"] = "127.0.0.1"
    os.environ["RANK"] = str(rank)
    os.environ["WORLD_SIZE"] = str(args.num_gpus)

    dist.init_process_group("nccl")
    # set up fairscale environment because some methods of the Lumina model need it,
    # though for single-GPU inference fairscale actually has no effect
    fs_init.initialize_model_parallel(args.num_gpus)
    torch.cuda.set_device(rank)

    train_args = torch.load(os.path.join(args.ckpt, "model_args.pth"))
    if dist.get_rank() == 0:
        print(
            "Loaded model arguments:",
            json.dumps(train_args.__dict__, indent=2)
        )

    if dist.get_rank() == 0:
        print(f"Creating lm: {train_args.lm}")

    dtype = {
        "bf16": torch.bfloat16,
        "fp16": torch.float16,
        "fp32": torch.float32
    }[args.precision]

    model_lm = AutoModelForCausalLM.from_pretrained(train_args.lm, torch_dtype=dtype, device_map="cuda")
    cap_feat_dim = model_lm.config.hidden_size
    if args.num_gpus > 1:
        raise NotImplementedError("Inference with >1 GPUs not yet supported")

    tokenizer = AutoTokenizer.from_pretrained(train_args.tokenizer_path, add_bos_token=True, add_eos_token=True)
    tokenizer.padding_side = 'right'

    if dist.get_rank() == 0:
        print(f"Creating vae: {train_args.vae}")
    vae = AutoencoderKL.from_pretrained(
        f"stabilityai/sd-vae-ft-{train_args.vae}"
        if train_args.vae != "sdxl"
        else "stabilityai/sdxl-vae",
        torch_dtype=torch.float32
    ).cuda()

    if dist.get_rank() == 0:
        print(f"Creating DiT: {train_args.model}")
    # latent_size = train_args.image_size // 8
    model = models.__dict__[train_args.model](
        qk_norm=train_args.qk_norm,
        cap_feat_dim=cap_feat_dim,
    )
    model.eval().to("cuda", dtype=dtype)

    assert train_args.model_parallel_size == args.num_gpus
    ckpt = torch.load(os.path.join(
        args.ckpt, f"consolidated{'_ema' if args.ema else ''}.{rank:02d}-of-{args.num_gpus:02d}.pth"
    ), map_location="cpu")
    model.load_state_dict(ckpt, strict=True)

    mp_barrier.wait()

    with torch.autocast("cuda", dtype):
        while True:
            (
                cap, resolution, num_sampling_steps, cfg_scale, solver, t_shift, seed, ntk_scaling, proportional_attn
            ) = request_queue.get()

            try:
                # begin sampler
                transport = create_transport(
                    args.path_type,
                    args.prediction,
                    args.loss_weight,
                    args.train_eps,
                    args.sample_eps

                )
                sampler = Sampler(transport)
                sample_fn = sampler.sample_ode(
                    sampling_method=solver,
                    num_steps=num_sampling_steps,
                    atol=args.atol,
                    rtol=args.rtol,
                    reverse=args.reverse,
                    time_shifting_factor=t_shift
                )
                # end sampler

                resolution = resolution.split(" ")[-1]
                w, h = resolution.split("x")
                w, h = int(w), int(h)
                latent_w, latent_h = w // 8, h // 8
                if int(seed) != 0:
                    torch.random.manual_seed(int(seed))
                z = torch.randn([1, 4, latent_h, latent_w], device="cuda").to(dtype)
                z = z.repeat(2, 1, 1, 1)

                cap_tok = tokenizer.encode(cap, truncation=False)
                null_cap_tok = tokenizer.encode("", truncation=False)
                tok = torch.zeros([2, max(len(cap_tok), len(null_cap_tok))], dtype=torch.long, device="cuda")
                tok_mask = torch.zeros_like(tok, dtype=torch.bool)
                tok[0, :len(cap_tok)] = torch.tensor(cap_tok)
                tok[1, :len(null_cap_tok)] = torch.tensor(null_cap_tok)
                tok_mask[0, :len(cap_tok)] = True
                tok_mask[1, :len(null_cap_tok)] = True

                cap_feats = model_lm.get_decoder()(input_ids=tok).last_hidden_state

                model_kwargs = dict(
                    cap_feats=cap_feats, cap_mask=tok_mask, cfg_scale=cfg_scale,
                )
                if proportional_attn:
                    model_kwargs['proportional_attn'] = True
                    model_kwargs['base_seqlen'] = (train_args.image_size // 16) ** 2 + (train_args.image_size // 16) * 2
                if ntk_scaling:
                    model_kwargs['ntk_factor'] = ((w // 16) * (h // 16)) / ((train_args.image_size // 16) ** 2)

                if dist.get_rank() == 0:
                    print(f"caption: {cap}")
                    print(f"num_sampling_steps: {num_sampling_steps}")
                    print(f"cfg_scale: {cfg_scale}")

                samples = sample_fn(z, model.forward_with_cfg, **model_kwargs)[-1]
                samples = samples[:1]

                factor = 0.18215 if train_args.vae != 'sdxl' else 0.13025
                print(f"vae factor: {factor}")
                samples = vae.decode(samples / factor).sample
                samples = (samples + 1.) / 2.
                samples.clamp_(0., 1.)
                img = to_pil_image(samples[0])

                if response_queue is not None:
                    response_queue.put(img)

            except Exception:
                print(traceback.format_exc())
                response_queue.put(ModelFailure())


def none_or_str(value):
    if value == 'None':
        return None
    return value


def parse_transport_args(parser):
    group = parser.add_argument_group("Transport arguments")
    group.add_argument("--path-type", type=str, default="Linear", choices=["Linear", "GVP", "VP"])
    group.add_argument("--prediction", type=str, default="velocity", choices=["velocity", "score", "noise"])
    group.add_argument("--loss-weight", type=none_or_str, default=None,
                       choices=[None, "velocity", "likelihood"])
    group.add_argument("--sample-eps", type=float)
    group.add_argument("--train-eps", type=float)


def parse_ode_args(parser):
    group = parser.add_argument_group("ODE arguments")
    group.add_argument("--atol", type=float, default=1e-6, help="Absolute tolerance")
    group.add_argument("--rtol", type=float, default=1e-3, help="Relative tolerance")
    group.add_argument("--reverse", action="store_true")
    group.add_argument("--likelihood", action="store_true")


def parse_sde_args(parser):
    group = parser.add_argument_group("SDE arguments")
    group.add_argument("--sampling-method", type=str, default="Euler", choices=["Euler", "Heun"])
    group.add_argument("--diffusion-form", type=str, default="sigma",
                       choices=["constant", "SBDM", "sigma", "linear", "decreasing",
                                "increasing-decreasing"],
                       help="form of diffusion coefficient in the SDE")
    group.add_argument("--diffusion-norm", type=float, default=1.0)
    group.add_argument("--last-step", type=none_or_str, default="Mean",
                       choices=[None, "Mean", "Tweedie", "Euler"],
                       help="form of last step taken in the SDE")
    group.add_argument("--last-step-size", type=float, default=0.04,
                       help="size of the last step taken")


def find_free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--num_gpus", type=int, default=1)
    parser.add_argument("--ckpt", type=str, required=True)
    parser.add_argument("--ema", action="store_true")
    parser.add_argument("--precision", default="bf16", choices=["bf16", "fp32"])

    parse_transport_args(parser)
    parse_ode_args(parser)

    args = parser.parse_known_args()[0]

    if args.num_gpus != 1:
        raise NotImplementedError("Multi-GPU Inference is not yet supported")

    master_port = find_free_port()

    processes = []
    request_queues = []
    response_queue = mp.Queue()
    mp_barrier = mp.Barrier(args.num_gpus + 1)
    for i in range(args.num_gpus):
        request_queues.append(mp.Queue())
        p = mp.Process(target=model_main,
                       args=(args, master_port, i, request_queues[i], response_queue if i == 0 else None, mp_barrier))
        p.start()
        processes.append(p)

    with gr.Blocks() as demo:
        with gr.Row():
            gr.Markdown(
                f"""# Lumina-T2I Image Generation Demo

**Model path:** {os.path.abspath(args.ckpt)}
 
**ema**: {args.ema}
                
**precision**: {args.precision}"""
            )
        with gr.Row():
            with gr.Column():
                cap = gr.Textbox(
                    lines=2, label="Caption", interactive=True,
                    value="A fluffy mouse holding a watermelon, in a magical and colorful setting, "
                          "illustrated in the style of Hayao Miyazaki anime by Studio Ghibli."
                )
                with gr.Row():
                    res_choices = (
                        ["1024x1024", "512x2048", "2048x512"] +
                        ["(Extrapolation) 1664x1664", "(Extrapolation) 1024x2048", "(Extrapolation) 2048x1024"]
                    )
                    resolution = gr.Dropdown(
                        value=res_choices[0],
                        choices=res_choices,
                        label="Resolution"
                    )
                with gr.Row():
                    num_sampling_steps = gr.Slider(
                        minimum=1, maximum=1000, value=60, interactive=True,
                        label="Sampling steps"
                    )
                    cfg_scale = gr.Slider(
                        minimum=1., maximum=20., value=4., interactive=True,
                        label="CFG scale"
                    )
                    solver = gr.Dropdown(
                        value="euler",
                        choices=["euler", "dopri5", "dopri8"],
                        label="solver"
                    )
                with gr.Row():
                    t_shift = gr.Slider(
                        minimum=1, maximum=20, value=4, step=1, interactive=True,
                        label="Time shift"
                    )
                    seed = gr.Slider(
                        minimum=0, maximum=int(1e5), value=1, step=1, interactive=True,
                        label="Seed (0 for random)"
                    )
                with gr.Accordion("Advanced Settings for Resolution Extrapolation", open=False):
                    with gr.Row():
                        ntk_scaling = gr.Checkbox(
                            value=True,
                            interactive=True,
                            label="ntk scaling",
                        )
                        proportional_attn = gr.Checkbox(
                            value=True,
                            interactive=True,
                            label="Proportional attention",
                        )
                with gr.Row():
                    submit_btn = gr.Button("Submit", variant="primary")
                    # reset_btn = gr.ClearButton([
                    #     cap, resolution,
                    #     num_sampling_steps, cfg_scale, solver,
                    #     t_shift, seed,
                    #     ntk_scaling, proportional_attn
                    # ])
            with gr.Column():
                output_img = gr.Image(label="Generated image", interactive=False)

        with gr.Row():
            gr.Examples(
                [
                    ["A fluffy mouse holding a watermelon, in a magical and colorful setting, illustrated in the style of Hayao Miyazaki anime by Studio Ghibli."],  # noqa
                    ["A humanoid eagle soldier of the First World War."],  # noqa
                    ["A cute Christmas mockup on an old wooden industrial desk table with Christmas decorations and bokeh lights in the background."],  # noqa
                    ["A scared cute rabbit in Happy Tree Friends style and punk vibe."],  # noqa
                    ["A front view of a romantic flower shop in France filled with various blooming flowers including lavenders and roses."],  # noqa
                    ["An old man, portrayed as a retro superhero, stands in the streets of New York City at night"],  # noqa
                ],
                [cap],
                label="Examples"
            )

        def on_submit(*args):
            for q in request_queues:
                q.put(args)
            result = response_queue.get()
            if isinstance(result, ModelFailure):
                raise RuntimeError
            return result

        submit_btn.click(
            on_submit,
            [cap, resolution, num_sampling_steps, cfg_scale, solver, t_shift, seed, ntk_scaling, proportional_attn],
            [output_img]
        )

    mp_barrier.wait()
    demo.queue().launch(
        share=True, server_name="0.0.0.0",
    )


if __name__ == "__main__":
    mp.set_start_method("spawn")
    main()
