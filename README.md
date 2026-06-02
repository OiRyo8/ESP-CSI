# esp-csi OiRyo8
----------
This is a starter project on CSI Data analysis for ESP32-S3 (ESP32 Family controllers)

*/main/app_main.c 
Contains the ESP32 script, that sends ICPM packets, collects data, merges the LTF and sends data to your PC.

*/tools/Phase_calc2.py 
Contains the parsing and the filtering sequences for the CSI Data
Filtering: CSI Ratio -> Phase unwrap + sanitize -> Median filter -> Interpolation -> Butterworth filter
                                                                               \                           (Work in pair for better data analysis)
                                                                                \--> Savitsky-Golay filter

*/tools/csi_analyzer.py 
Analyzes filtered data via FFT and Standart deviation? Able to analyze tour presence and movement and count steps and breathing

*/tools/csi_calibration.py
Measures the empty room noise and uses that data in the analysis sequence