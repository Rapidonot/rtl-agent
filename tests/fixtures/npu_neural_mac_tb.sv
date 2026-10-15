`timescale 1ns/1ps
module tb;
    reg clk = 0;
    reg clear = 0;
    reg en = 0;
    reg signed [7:0] a = 0, b = 0;
    wire signed [19:0] acc;
    integer expected = 0;
    integer i;
    npu_neural_mac dut (.clk(clk), .clear(clear), .en(en), .a(a), .b(b), .acc(acc));
    always #5 clk = ~clk;

    task step(input reg c, input reg e, input reg signed [7:0] x, y);
        integer product;
        begin
            @(negedge clk);
            clear = c; en = e; a = x; b = y;
            product = x;
            product = product * y;
            if (c) expected = 0;
            else if (e) expected = (expected + product) & 32'h000fffff;
            @(posedge clk);
            #1;
            if (acc !== 20'(expected))
                $fatal(1, "MAC mismatch time=%0t expected=%0d actual=%0d",
                       $time, $signed(20'(expected)), acc);
        end
    endtask

    initial begin
        $dumpfile("waves.vcd");
        $dumpvars(0, tb);
        step(1, 1, 8'sd7, 8'sd9);
        step(0, 1, 8'sd3, 8'sd4);
        step(0, 1, -8'sd5, 8'sd6);
        step(0, 1, -8'sd7, -8'sd8);
        step(0, 0, 8'sd127, 8'sd127);
        step(0, 1, 8'sd0, -8'sd128);
        step(1, 0, 8'sd127, 8'sd127);
        for (i = 0; i < 65; i = i + 1) step(0, 1, -8'sd128, -8'sd128);
        step(1, 1, 8'sd127, 8'sd127);
        for (i = 0; i < 65; i = i + 1) step(0, 1, -8'sd128, 8'sd127);
        step(0, 0, 8'sd12, 8'sd13);
        $display("RTL_AGENT_PASS");
        $finish;
    end
    initial begin
        #3000;
        $fatal(1, "testbench watchdog timeout");
    end
endmodule
