#!/bin/bash

# Function to check input values
check_input() {
    if [ -z "$1" ] || [ -z "$2" ]; then
        echo "Error: No parameter was added on invocation."
        echo "Usage: $0 <s3-bucket> <s3-key> <python-version>"
        echo "Example: $0 my-s3-bucket my-key-assets py-version"
        echo "Python Version: default is 3.11 if omitted"
        exit 1
    fi

    # Default Python version
    PYTHON_VERSION="3.11"

    # Check if the third parameter is provided
    if [ -n "$3" ]; then
        PYTHON_VERSION="$3"
    fi

    echo "Using Python version: $PYTHON_VERSION"
}

# Calling function to validate params
check_input "$@"

# Adding parameters into variables
BUCKET="$1"
KEY="$2"

echo "Bucket: $BUCKET"
echo "Key: $KEY"

# Build lambda.zip (deps resolved for the Lambda runtime; works on Windows and Linux)
./pack_lambda.sh "$PYTHON_VERSION"

# Uploading the lambda.zip to the specified S3 bucket and key
echo "Uploading lambda.zip into: $BUCKET"
aws s3 cp lambda.zip s3://$BUCKET/$KEY/lambda.zip
rm lambda.zip

REGION=$(aws configure get region)
CFN_FILE=template.yaml

echo "Creating CloudFormation Stack"
aws cloudformation create-stack --region $REGION \
 --stack-name aws-whatsapp-stack \
 --template-body file://$CFN_FILE \
 --parameters ParameterKey=S3BucketName,ParameterValue=$BUCKET \
              ParameterKey=S3BucketPath,ParameterValue=$KEY \
              ParameterKey=PythonVersion,ParameterValue=python$PYTHON_VERSION \
 --capabilities CAPABILITY_IAM