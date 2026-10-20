I built RTL Agent because I wanted to find out how much AI could help in developing chips. The first version generated SystemVerilog and a testbench, ran compiler and simulation checks, and sent failures back to the model for repair. I kept the testbench fixed so repairs had to change the design.

I then realised that simply locking the testbench may not be enough since the test and RTL is generated in the same call. The agent was made to write and save its tests before generating any RTL ( in a separate call). I added synthesis checks for inferred latches and gave repairs waveform evidence. But the tests started causing more issues. I then refined the prompts regarding widths, clock edges, reset behaviour, and addressed missing newlines in code so formatting would not waste a repair attempt.

An interesting result came from benchmarking the agent using VerilogEval’s official tests. Of 98 evaluated problems, it accepted correct RTL for 80, but also reported ten false passes, due to a faulty testbench. This made me realise that the testbench may be equally important as the agent itself, and should be further improved. 

Building the agent taught me the importance of considering a system as a whole, and not simply focus my efforts on one part. I could improve the RTL generation and still lose a correct design through a faulty test or an overly restrictive check.
