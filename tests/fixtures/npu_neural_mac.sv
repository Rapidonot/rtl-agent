`timescale 1ns/1ps
module npu_neural_mac (
    input wire clk, clear, en,
    input wire signed [7:0] a, b,
    output reg signed [19:0] acc
);
    wire signed [15:0] product = a * b;
    always @(posedge clk) begin
        if (clear) acc <= 20'sd0;
        else if (en) acc <= acc + {{4{product[15]}}, product};
    end
endmodule
